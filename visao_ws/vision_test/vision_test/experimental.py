import numpy as np
import random
import torch
import torch.nn as nn
import sys
sys.path.insert(0, '/home/robofei/Desktop/visao_ws/vision_test/vision_test')

from .common import Conv, DWConv
from .utils.google_utils import attempt_download


class CrossConv(nn.Module):
    # Cross Convolution Downsample
    def __init__(self, c1, c2, k=3, s=1, g=1, e=1.0, shortcut=False):
        # ch_in, ch_out, kernel, stride, groups, expansion, shortcut
        super(CrossConv, self).__init__()
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, c_, (1, k), (1, s))
        self.cv2 = Conv(c_, c2, (k, 1), (s, 1), g=g)
        self.add = shortcut and c1 == c2

    def forward(self, x):
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))


class Sum(nn.Module):
    # Weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, n, weight=False):  # n: number of inputs
        super(Sum, self).__init__()
        self.weight = weight  # apply weights boolean
        self.iter = range(n - 1)  # iter object
        if weight:
            self.w = nn.Parameter(-torch.arange(1., n) / 2, requires_grad=True)  # layer weights

    def forward(self, x):
        y = x[0]  # no weight
        if self.weight:
            w = torch.sigmoid(self.w) * 2
            for i in self.iter:
                y = y + x[i + 1] * w[i]
        else:
            for i in self.iter:
                y = y + x[i + 1]
        return y


class MixConv2d(nn.Module):
    # Mixed Depthwise Conv https://arxiv.org/abs/1907.09595
    def __init__(self, c1, c2, k=(1, 3), s=1, equal_ch=True):
        super(MixConv2d, self).__init__()
        groups = len(k)
        if equal_ch:  # equal c_ per group
            i = torch.linspace(0, groups - 1E-6, c2).floor()  # c2 indices
            c_ = [(i == g).sum() for g in range(groups)]  # intermediate channels
        else:  # equal weight.numel() per group
            b = [c2] + [0] * groups
            a = np.eye(groups + 1, groups, k=-1)
            a -= np.roll(a, 1, axis=1)
            a *= np.array(k) ** 2
            a[0] = 1
            c_ = np.linalg.lstsq(a, b, rcond=None)[0].round()  # solve for equal weight indices, ax = b

        self.m = nn.ModuleList([nn.Conv2d(c1, int(c_[g]), k[g], s, k[g] // 2, bias=False) for g in range(groups)])
        self.bn = nn.BatchNorm2d(c2)
        self.act = nn.LeakyReLU(0.1, inplace=True)

    def forward(self, x):
        return x + self.act(self.bn(torch.cat([m(x) for m in self.m], 1)))


class Ensemble(nn.ModuleList):
    # Ensemble of models
    def __init__(self):
        super(Ensemble, self).__init__()

    def forward(self, x, augment=False):
        y = []
        for module in self:
            y.append(module(x, augment)[0])
        # y = torch.stack(y).max(0)[0]  # max ensemble
        # y = torch.stack(y).mean(0)  # mean ensemble
        y = torch.cat(y, 1)  # nms ensemble
        return y, None  # inference, train output





class ORT_NMS(torch.autograd.Function):
    '''ONNX-Runtime NMS operation'''
    @staticmethod
    def forward(ctx,
                boxes,
                scores,
                max_output_boxes_per_class=torch.tensor([100]),
                iou_threshold=torch.tensor([0.45]),
                score_threshold=torch.tensor([0.25])):
        device = boxes.device
        batch = scores.shape[0]
        num_det = random.randint(0, 100)
        batches = torch.randint(0, batch, (num_det,)).sort()[0].to(device)
        idxs = torch.arange(100, 100 + num_det).to(device)
        zeros = torch.zeros((num_det,), dtype=torch.int64).to(device)
        selected_indices = torch.cat([batches[None], zeros[None], idxs[None]], 0).T.contiguous()
        selected_indices = selected_indices.to(torch.int64)
        return selected_indices

    @staticmethod
    def symbolic(g, boxes, scores, max_output_boxes_per_class, iou_threshold, score_threshold):
        return g.op("NonMaxSuppression", boxes, scores, max_output_boxes_per_class, iou_threshold, score_threshold)


class TRT_NMS(torch.autograd.Function):
    '''TensorRT NMS operation'''
    @staticmethod
    def forward(
        ctx,
        boxes,
        scores,
        background_class=-1,
        box_coding=1,
        iou_threshold=0.45,
        max_output_boxes=100,
        plugin_version="1",
        score_activation=0,
        score_threshold=0.25,
    ):
        batch_size, num_boxes, num_classes = scores.shape
        num_det = torch.randint(0, max_output_boxes, (batch_size, 1), dtype=torch.int32)
        det_boxes = torch.randn(batch_size, max_output_boxes, 4)
        det_scores = torch.randn(batch_size, max_output_boxes)
        det_classes = torch.randint(0, num_classes, (batch_size, max_output_boxes), dtype=torch.int32)
        return num_det, det_boxes, det_scores, det_classes

    @staticmethod
    def symbolic(g,
                 boxes,
                 scores,
                 background_class=-1,
                 box_coding=1,
                 iou_threshold=0.45,
                 max_output_boxes=100,
                 plugin_version="1",
                 score_activation=0,
                 score_threshold=0.25):
        out = g.op("TRT::EfficientNMS_TRT",
                   boxes,
                   scores,
                   background_class_i=background_class,
                   box_coding_i=box_coding,
                   iou_threshold_f=iou_threshold,
                   max_output_boxes_i=max_output_boxes,
                   plugin_version_s=plugin_version,
                   score_activation_i=score_activation,
                   score_threshold_f=score_threshold,
                   outputs=4)
        nums, boxes, scores, classes = out
        return nums, boxes, scores, classes


class ONNX_ORT(nn.Module):
    '''onnx module with ONNX-Runtime NMS operation.'''
    def __init__(self, max_obj=100, iou_thres=0.45, score_thres=0.25, max_wh=640, device=None):
        super().__init__()
        self.device = device if device else torch.device("cpu")
        self.max_obj = torch.tensor([max_obj]).to(device)
        self.iou_threshold = torch.tensor([iou_thres]).to(device)
        self.score_threshold = torch.tensor([score_thres]).to(device)
        self.max_wh = max_wh # if max_wh != 0 : non-agnostic else : agnostic
        self.convert_matrix = torch.tensor([[1, 0, 1, 0], [0, 1, 0, 1], [-0.5, 0, 0.5, 0], [0, -0.5, 0, 0.5]],
                                           dtype=torch.float32,
                                           device=self.device)

    def forward(self, x):
        boxes = x[:, :, :4]
        conf = x[:, :, 4:5]
        scores = x[:, :, 5:]
        scores *= conf
        boxes @= self.convert_matrix
        max_score, category_id = scores.max(2, keepdim=True)
        dis = category_id.float() * self.max_wh
        nmsbox = boxes + dis
        max_score_tp = max_score.transpose(1, 2).contiguous()
        selected_indices = ORT_NMS.apply(nmsbox, max_score_tp, self.max_obj, self.iou_threshold, self.score_threshold)
        X, Y = selected_indices[:, 0], selected_indices[:, 2]
        selected_boxes = boxes[X, Y, :]
        selected_categories = category_id[X, Y, :].float()
        selected_scores = max_score[X, Y, :]
        X = X.unsqueeze(1).float()
        return torch.cat([X, selected_boxes, selected_categories, selected_scores], 1)

class ONNX_TRT(nn.Module):
    '''onnx module with TensorRT NMS operation.'''
    def __init__(self, max_obj=100, iou_thres=0.45, score_thres=0.25, max_wh=None ,device=None):
        super().__init__()
        assert max_wh is None
        self.device = device if device else torch.device('cpu')
        self.background_class = -1,
        self.box_coding = 1,
        self.iou_threshold = iou_thres
        self.max_obj = max_obj
        self.plugin_version = '1'
        self.score_activation = 0
        self.score_threshold = score_thres

    def forward(self, x):
        boxes = x[:, :, :4]
        conf = x[:, :, 4:5]
        scores = x[:, :, 5:]
        scores *= conf
        num_det, det_boxes, det_scores, det_classes = TRT_NMS.apply(boxes, scores, self.background_class, self.box_coding,
                                                                    self.iou_threshold, self.max_obj,
                                                                    self.plugin_version, self.score_activation,
                                                                    self.score_threshold)
        return num_det, det_boxes, det_scores, det_classes


class End2End(nn.Module):
    '''export onnx or tensorrt model with NMS operation.'''
    def __init__(self, model, max_obj=100, iou_thres=0.45, score_thres=0.25, max_wh=None, device=None):
        super().__init__()
        device = device if device else torch.device('cpu')
        assert isinstance(max_wh,(int)) or max_wh is None
        self.model = model.to(device)
        self.model.model[-1].end2end = True
        self.patch_model = ONNX_TRT if max_wh is None else ONNX_ORT
        self.end2end = self.patch_model(max_obj, iou_thres, score_thres, max_wh, device)
        self.end2end.eval()

    def forward(self, x):
        x = self.model(x)
        x = self.end2end(x)
        return x





def attempt_load(weights, map_location=None):
    # Loads an ensemble of models weights=[a,b,c] or a single model weights=[a] or weights=a
    model = Ensemble()
    for w in weights if isinstance(weights, list) else [weights]:
        print(w)
        attempt_download("/home/robofei/Desktop/visao_ws/vision_test/vision_test/best.pt")
        ckpt = load(w, map_location=map_location)  # load
        model.append(ckpt['ema' if ckpt.get('ema') else 'model'].float().fuse().eval())  # FP32 model
    
    # Compatibility updates
    for m in model.modules():
        if type(m) in [nn.Hardswish, nn.LeakyReLU, nn.ReLU, nn.ReLU6, nn.SiLU]:
            m.inplace = True  # pytorch 1.7.0 compatibility
        elif type(m) is nn.Upsample:
            m.recompute_scale_factor = None  # torch 1.11.0 compatibility
        elif type(m) is Conv:
            m._non_persistent_buffers_set = set()  # pytorch 1.6.0 compatibility
    
    if len(model) == 1:
        return model[-1]  # return model
    else:
        print('Ensemble created with %s\n' % weights)
        for k in ['names', 'stride']:
            setattr(model, k, getattr(model[-1], k))
        return model  # return ensemble
import difflib
import os
import io
import shutil
import struct
import sys
import torch
import tarfile
import tempfile
import warnings
from contextlib import closing, contextmanager
from ._utils import _import_dotted_name
from ._six import string_classes as _string_classes
from torch._sources import get_source_lines_and_file
from torch.types import Storage
from torch.storage import _get_dtype_from_pickle_storage_type
from typing import Any, BinaryIO, cast, Dict, Optional, Type, Tuple, Union, IO
import copyreg
import pickle
import pathlib
import sys
sys.path.insert(1, '/home/robofei/Desktop/visao_ws/vision_test/best.pt')

DEFAULT_PROTOCOL = 2

LONG_SIZE = struct.Struct('=l').size
INT_SIZE = struct.Struct('=i').size
SHORT_SIZE = struct.Struct('=h').size

MAGIC_NUMBER = 0x1950a86a20f9469cfc6c
PROTOCOL_VERSION = 1001
STORAGE_KEY_SEPARATOR = ','

class SourceChangeWarning(Warning):
    pass


@contextmanager
def mkdtemp():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path)


_package_registry = []


def _is_zipfile(f) -> bool:
    # This is a stricter implementation than zipfile.is_zipfile().
    # zipfile.is_zipfile() is True if the magic number appears anywhere in the
    # binary. Since we expect the files here to be generated by torch.save or
    # torch.jit.save, it's safe to only check the start bytes and avoid
    # collisions and assume the zip has only 1 file.
    # See bugs.python.org/issue28494.

    # Read the first 4 bytes of the file
    read_bytes = []
    start = f.tell()

    byte = f.read(1)
    while byte != "":
        read_bytes.append(byte)
        if len(read_bytes) == 4:
            break
        byte = f.read(1)
    f.seek(start)

    local_header_magic_number = [b'P', b'K', b'\x03', b'\x04']
    return read_bytes == local_header_magic_number


def register_package(priority, tagger, deserializer):
    queue_elem = (priority, tagger, deserializer)
    _package_registry.append(queue_elem)
    _package_registry.sort()


def check_module_version_greater_or_equal(module, req_version_tuple, error_if_malformed=True):
    '''
    Check if a module's version satisfies requirements

    Usually, a module's version string will be like 'x.y.z', which would be represented
    as a tuple (x, y, z), but sometimes it could be an unexpected format. If the version
    string does not match the given tuple's format up to the length of the tuple, then
    error and exit or emit a warning.

    Args:
        module: the module to check the version of
        req_version_tuple: tuple (usually of ints) representing the required version
        error_if_malformed: whether we should exit if module version string is malformed

    Returns:
        requirement_is_met: bool
    '''
    try:
        version_strs = module.__version__.split('.')
        # Cast module version fields to match the types of the required version
        module_version = tuple(
            type(req_field)(version_strs[idx]) for idx, req_field in enumerate(req_version_tuple)
        )
        requirement_is_met = module_version >= req_version_tuple

    except Exception as e:
        message = (
            "'%s' module version string is malformed '%s' and cannot be compared"
            " with tuple %s"
        ) % (
            module.__name__, module.__version__, str(req_version_tuple)
        )
        if error_if_malformed:
            raise RuntimeError(message) from e
        else:
            warnings.warn(message + ', but continuing assuming that requirement is met')
            requirement_is_met = True

    return requirement_is_met


def _cpu_tag(obj):
    if obj.device.type == 'cpu':
        return 'cpu'


def _cuda_tag(obj):
    if obj.device.type == 'cuda':
        return 'cuda:' + str(obj.device.index)


def _cpu_deserialize(obj, location):
    if location == 'cpu':
        return obj


def validate_cuda_device(location):
    device = torch.cuda._utils._get_device_index(location, True)

    if not torch.cuda.is_available():
        raise RuntimeError('Attempting to deserialize object on a CUDA '
                           'device but torch.cuda.is_available() is False. '
                           'If you are running on a CPU-only machine, '
                           'please use torch.load with map_location=torch.device(\'cpu\') '
                           'to map your storages to the CPU.')
    device_count = torch.cuda.device_count()
    if device >= device_count:
        raise RuntimeError('Attempting to deserialize object on CUDA device '
                           f'{device} but torch.cuda.device_count() is {device_count}. Please use '
                           'torch.load with map_location to map your storages '
                           'to an existing device.')
    return device


def _cuda_deserialize(obj, location):
    if location.startswith('cuda'):
        device = validate_cuda_device(location)
        if getattr(obj, "_torch_load_uninitialized", False):
            with torch.cuda.device(device):
                return torch._UntypedStorage(obj.nbytes(), device=torch.device(location))
        else:
            return obj.cuda(device)


register_package(10, _cpu_tag, _cpu_deserialize)
register_package(20, _cuda_tag, _cuda_deserialize)


def location_tag(storage: Union[Storage, torch.storage._TypedStorage, torch._UntypedStorage]):
    for _, tagger, _ in _package_registry:
        location = tagger(storage)
        if location:
            return location
    raise RuntimeError("don't know how to determine data location of "
                       + torch.typename(storage))


def default_restore_location(storage, location):
    for _, _, fn in _package_registry:
        result = fn(storage, location)
        if result is not None:
            return result
    raise RuntimeError("don't know how to restore data location of "
                       + torch.typename(storage) + " (tagged with "
                       + location + ")")


def normalize_storage_type(storage_type):
    return getattr(torch, storage_type.__name__)


def storage_to_tensor_type(storage):
    storage_type = type(storage)
    module = _import_dotted_name(storage_type.__module__)
    return getattr(module, storage_type.__name__.replace('Storage', 'Tensor'))


def _is_path(name_or_buffer):
    return isinstance(name_or_buffer, str) or \
        isinstance(name_or_buffer, pathlib.Path)


class _opener(object):
    def __init__(self, file_like):
        self.file_like = file_like

    def __enter__(self):
        return self.file_like

    def __exit__(self, *args):
        pass


class _open_file(_opener):
    def __init__(self, name, mode):
        super(_open_file, self).__init__(open(name, mode))

    def __exit__(self, *args):
        self.file_like.close()


class _open_buffer_reader(_opener):
    def __init__(self, buffer):
        super(_open_buffer_reader, self).__init__(buffer)
        _check_seekable(buffer)


class _open_buffer_writer(_opener):
    def __exit__(self, *args):
        self.file_like.flush()


def _open_file_like(name_or_buffer, mode):
    if _is_path(name_or_buffer):
        return _open_file(name_or_buffer, mode)
    else:
        if 'w' in mode:
            return _open_buffer_writer(name_or_buffer)
        elif 'r' in mode:
            return _open_buffer_reader(name_or_buffer)
        else:
            raise RuntimeError(f"Expected 'r' or 'w' in mode but got {mode}")


class _open_zipfile_reader(_opener):
    def __init__(self, name_or_buffer) -> None:
        super(_open_zipfile_reader, self).__init__(torch._C.PyTorchFileReader(name_or_buffer))


class _open_zipfile_writer_file(_opener):
    def __init__(self, name) -> None:
        super(_open_zipfile_writer_file, self).__init__(torch._C.PyTorchFileWriter(str(name)))

    def __exit__(self, *args) -> None:
        self.file_like.write_end_of_file()


class _open_zipfile_writer_buffer(_opener):
    def __init__(self, buffer) -> None:
        self.buffer = buffer
        super(_open_zipfile_writer_buffer, self).__init__(torch._C.PyTorchFileWriter(buffer))

    def __exit__(self, *args) -> None:
        self.file_like.write_end_of_file()
        self.buffer.flush()


def _open_zipfile_writer(name_or_buffer):
    container: Type[_opener]
    if _is_path(name_or_buffer):
        container = _open_zipfile_writer_file
    else:
        container = _open_zipfile_writer_buffer
    return container(name_or_buffer)


def _is_compressed_file(f) -> bool:
    compress_modules = ['gzip']
    try:
        return f.__module__ in compress_modules
    except AttributeError:
        return False


def _should_read_directly(f):
    """
    Checks if f is a file that should be read directly. It should be read
    directly if it is backed by a real file (has a fileno) and is not a
    a compressed file (e.g. gzip)
    """
    if _is_compressed_file(f):
        return False
    try:
        return f.fileno() >= 0
    except io.UnsupportedOperation:
        return False
    except AttributeError:
        return False


def _check_seekable(f) -> bool:

    def raise_err_msg(patterns, e):
        for p in patterns:
            if p in str(e):
                msg = (str(e) + ". You can only torch.load from a file that is seekable."
                                + " Please pre-load the data into a buffer like io.BytesIO and"
                                + " try to load from it instead.")
                raise type(e)(msg)
        raise e

    try:
        f.seek(f.tell())
        return True
    except (io.UnsupportedOperation, AttributeError) as e:
        raise_err_msg(["seek", "tell"], e)
    return False

def _check_dill_version(pickle_module) -> None:
    '''Checks if using dill as the pickle module, and if so, checks if it is the correct version.
    If dill version is lower than 0.3.1, a ValueError is raised.

    Args:
        pickle_module: module used for pickling metadata and objects

    '''
    if pickle_module.__name__ == 'dill':
        required_dill_version = (0, 3, 1)
        if not check_module_version_greater_or_equal(pickle_module, required_dill_version, False):
            raise ValueError((
                "'torch' supports dill >= %s, but you have dill %s."
                " Please upgrade dill or switch to 'pickle'"
            ) % (
                '.'.join([str(num) for num in required_dill_version]),
                pickle_module.__version__
            ))

def save(obj, f: Union[str, os.PathLike, BinaryIO, IO[bytes]],
         pickle_module=pickle, pickle_protocol=DEFAULT_PROTOCOL, _use_new_zipfile_serialization=True) -> None:
    # Reference: https://github.com/pytorch/pytorch/issues/54354
    # The first line of this docstring overrides the one Sphinx generates for the
    # documentation. We need it so that Sphinx doesn't leak `pickle`s path from
    # the build environment (e.g. `<module 'pickle' from '/leaked/path').

    """save(obj, f, pickle_module=pickle, pickle_protocol=DEFAULT_PROTOCOL, _use_new_zipfile_serialization=True)

    Saves an object to a disk file.

    See also: :ref:`saving-loading-tensors`

    Args:
        obj: saved object
        f: a file-like object (has to implement write and flush) or a string or
           os.PathLike object containing a file name
        pickle_module: module used for pickling metadata and objects
        pickle_protocol: can be specified to override the default protocol

    .. note::
        A common PyTorch convention is to save tensors using .pt file extension.

    .. note::
        PyTorch preserves storage sharing across serialization. See
        :ref:`preserve-storage-sharing` for more details.

    .. note::
        The 1.6 release of PyTorch switched ``torch.save`` to use a new
        zipfile-based file format. ``torch.load`` still retains the ability to
        load files in the old format. If for any reason you want ``torch.save``
        to use the old format, pass the kwarg ``_use_new_zipfile_serialization=False``.

    Example:
        >>> # Save to file
        >>> x = torch.tensor([0, 1, 2, 3, 4])
        >>> torch.save(x, 'tensor.pt')
        >>> # Save to io.BytesIO buffer
        >>> buffer = io.BytesIO()
        >>> torch.save(x, buffer)
    """
    _check_dill_version(pickle_module)

    with _open_file_like(f, 'wb') as opened_file:
        if _use_new_zipfile_serialization:
            with _open_zipfile_writer(opened_file) as opened_zipfile:
                _save(obj, opened_zipfile, pickle_module, pickle_protocol)
                return
        _legacy_save(obj, opened_file, pickle_module, pickle_protocol)


def _legacy_save(obj, f, pickle_module, pickle_protocol) -> None:
    import torch.nn as nn
    serialized_container_types = {}
    serialized_storages = {}

    # Since loading storages that view the same data with different dtypes is
    # not supported, we need to keep track of the dtype associated with each
    # storage data_ptr and throw an error if the dtype is ever different.
    # TODO: This feature could be added in the future
    storage_dtypes: Dict[int, torch.dtype] = {}

    def persistent_id(obj: Any) -> Optional[Tuple]:
        # FIXME: the docs say that persistent_id should only return a string
        # but torch store returns tuples. This works only in the binary protocol
        # see
        # https://docs.python.org/2/library/pickle.html#pickling-and-unpickling-external-objects
        # https://github.com/python/cpython/blob/master/Lib/pickle.py#L527-L537
        if isinstance(obj, type) and issubclass(obj, nn.Module):
            if obj in serialized_container_types:
                return None
            serialized_container_types[obj] = True
            source_file = source = None
            try:
                source_lines, _, source_file = get_source_lines_and_file(obj)
                source = ''.join(source_lines)
            except Exception:  # saving the source is optional, so we can ignore any errors
                warnings.warn("Couldn't retrieve source code for container of "
                              "type " + obj.__name__ + ". It won't be checked "
                              "for correctness upon loading.")
            return ('module', obj, source_file, source)

        if isinstance(obj, torch.storage._TypedStorage) or torch.is_storage(obj):
            storage: torch._UntypedStorage

            if isinstance(obj, torch.storage._TypedStorage):
                # TODO: Once we decide to break serialization FC, this case
                # can be deleted
                storage = obj._storage
                storage_dtype = obj.dtype
                storage_type_str = obj.pickle_storage_type()
                storage_type = getattr(torch, storage_type_str)
                dtype = obj.dtype
                storage_numel = obj.size()

            elif isinstance(obj, torch._UntypedStorage):
                storage = obj
                storage_dtype = torch.uint8
                storage_type = normalize_storage_type(type(obj))
                dtype = torch.uint8
                storage_numel = storage.nbytes()
            else:
                raise TypeError(f'type not recognized: {type(obj)}')

            # If storage is allocated, ensure that any other saved storages
            # pointing to the same data all have the same dtype. If storage is
            # not allocated, don't perform this check
            if storage.data_ptr() != 0:
                if storage.data_ptr() in storage_dtypes:
                    if storage_dtype != storage_dtypes[storage.data_ptr()]:
                        raise RuntimeError(
                            'Cannot save multiple tensors or storages that '
                            'view the same data as different types')
                else:
                    storage_dtypes[storage.data_ptr()] = storage_dtype

            view_metadata: Optional[Tuple[str, int, int]]

            # Offset is always 0, but we keep it for backwards compatibility
            # with the old serialization format (which supported storage views)
            offset = 0
            storage_key = str(storage._cdata)
            location = location_tag(storage)

            # TODO: There's an issue here with FC. It might be impossible to
            # solve, but it's worth noting. Imagine we save a list `[storage,
            # tensor]`, where `tensor.storage()` is the same as `storage`, and
            # `tensor.element_size() > 1`. Let's say that `tensor.dtype ==
            # torch.float`.  The storage will be serialized with element size
            # of 1, since we're choosing to serialize the first occurance of
            # a duplicate storage. Since this legacy serialization format saves
            # the numel of the storage, rather than nbytes directly, we'll be
            # effectively saving nbytes in this case.  We'll be able to load it
            # and the tensor back up with no problems in _this_ and future
            # versions of pytorch, but in older versions, here's the problem:
            # the storage will be loaded up as a _UntypedStorage, and then the
            # FloatTensor will loaded and the _UntypedStorage will be assigned to
            # it. Since the storage dtype does not match the tensor dtype, this
            # will cause an error.  If we reverse the list, like `[tensor,
            # storage]`, then we will save the `tensor.storage()` as a faked
            # `FloatStorage`, and the saved size will be the correct
            # dtype-specific numel count that old versions expect. `tensor`
            # will be able to load up properly in old versions, pointing to
            # a FloatStorage. However, `storage` is still being translated to
            # a _UntypedStorage, and it will try to resolve to the same
            # FloatStorage that `tensor` contains. This will also cause an
            # error. It doesn't seem like there's any way around this.
            # Probably, we just cannot maintain FC for the legacy format if the
            # saved list contains both a tensor and a storage that point to the
            # same data.  We should still be able to maintain FC for lists of
            # just tensors, as long as all views share the same dtype as the
            # tensor they are viewing.

            if storage_key not in serialized_storages:
                serialized_storages[storage_key] = (storage, dtype)
            is_view = storage._cdata != storage._cdata
            if is_view:
                view_metadata = (str(storage._cdata), offset, storage.nbytes())
            else:
                view_metadata = None

            res = ('storage',
                   storage_type,
                   storage_key,
                   location,
                   storage_numel,
                   view_metadata)
            return res
        return None

    sys_info = dict(
        protocol_version=PROTOCOL_VERSION,
        little_endian=sys.byteorder == 'little',
        type_sizes=dict(
            short=SHORT_SIZE,
            int=INT_SIZE,
            long=LONG_SIZE,
        ),
    )

    pickle_module.dump(MAGIC_NUMBER, f, protocol=pickle_protocol)
    pickle_module.dump(PROTOCOL_VERSION, f, protocol=pickle_protocol)
    pickle_module.dump(sys_info, f, protocol=pickle_protocol)
    pickler = pickle_module.Pickler(f, protocol=pickle_protocol)
    pickler.persistent_id = persistent_id
    pickler.dump(obj)

    serialized_storage_keys = sorted(serialized_storages.keys())
    pickle_module.dump(serialized_storage_keys, f, protocol=pickle_protocol)
    f.flush()
    for key in serialized_storage_keys:
        storage, dtype = serialized_storages[key]
        storage._write_file(f, _should_read_directly(f), True, torch._utils._element_size(dtype))


def _save(obj, zip_file, pickle_module, pickle_protocol):
    serialized_storages = {}
    id_map: Dict[int, str] = {}

    # Since loading storages that view the same data with different dtypes is
    # not supported, we need to keep track of the dtype associated with each
    # storage data_ptr and throw an error if the dtype is ever different.
    # TODO: This feature could be added in the future
    storage_dtypes: Dict[int, torch.dtype] = {}

    def persistent_id(obj):
        # FIXME: the docs say that persistent_id should only return a string
        # but torch store returns tuples. This works only in the binary protocol
        # see
        # https://docs.python.org/2/library/pickle.html#pickling-and-unpickling-external-objects
        # https://github.com/python/cpython/blob/master/Lib/pickle.py#L527-L537
        if isinstance(obj, torch.storage._TypedStorage) or torch.is_storage(obj):

            if isinstance(obj, torch.storage._TypedStorage):
                # TODO: Once we decide to break serialization FC, this case
                # can be deleted
                storage = obj._storage
                storage_dtype = obj.dtype
                storage_type_str = obj.pickle_storage_type()
                storage_type = getattr(torch, storage_type_str)
                storage_numel = obj.size()

            else:
                storage = obj
                storage_dtype = torch.uint8
                storage_type = normalize_storage_type(type(obj))
                storage_numel = storage.nbytes()

            # If storage is allocated, ensure that any other saved storages
            # pointing to the same data all have the same dtype. If storage is
            # not allocated, don't perform this check
            if storage.data_ptr() != 0:
                if storage.data_ptr() in storage_dtypes:
                    if storage_dtype != storage_dtypes[storage.data_ptr()]:
                        raise RuntimeError(
                            'Cannot save multiple tensors or storages that '
                            'view the same data as different types')
                else:
                    storage_dtypes[storage.data_ptr()] = storage_dtype

            storage_key = id_map.setdefault(storage._cdata, str(len(id_map)))
            location = location_tag(storage)
            serialized_storages[storage_key] = storage

            return ('storage',
                    storage_type,
                    storage_key,
                    location,
                    storage_numel)

        return None

    # Write the pickle data for `obj`
    data_buf = io.BytesIO()
    pickler = pickle_module.Pickler(data_buf, protocol=pickle_protocol)
    pickler.persistent_id = persistent_id
    pickler.dump(obj)
    data_value = data_buf.getvalue()
    zip_file.write_record('data.pkl', data_value, len(data_value))

    # Write each tensor to a file named tensor/the_tensor_key in the zip archive
    for key in sorted(serialized_storages.keys()):
        name = f'data/{key}'
        storage = serialized_storages[key]
        # given that we copy things around anyway, we might use storage.cpu()
        # this means to that to get tensors serialized, you need to implement
        # .cpu() on the underlying Storage
        if storage.device.type != 'cpu':
            storage = storage.cpu()
        # Now that it is on the CPU we can directly copy it into the zip file
        num_bytes = storage.nbytes()
        zip_file.write_record(name, storage.data_ptr(), num_bytes)


def load(f, map_location=None, pickle_module=pickle, **pickle_load_args):
    # Reference: https://github.com/pytorch/pytorch/issues/54354
    # The first line of this docstring overrides the one Sphinx generates for the
    # documentation. We need it so that Sphinx doesn't leak `pickle`s path from
    # the build environment (e.g. `<module 'pickle' from '/leaked/path').

    """load(f, map_location=None, pickle_module=pickle, **pickle_load_args)

    Loads an object saved with :func:`torch.save` from a file.

    :func:`torch.load` uses Python's unpickling facilities but treats storages,
    which underlie tensors, specially. They are first deserialized on the
    CPU and are then moved to the device they were saved from. If this fails
    (e.g. because the run time system doesn't have certain devices), an exception
    is raised. However, storages can be dynamically remapped to an alternative
    set of devices using the :attr:`map_location` argument.

    If :attr:`map_location` is a callable, it will be called once for each serialized
    storage with two arguments: storage and location. The storage argument
    will be the initial deserialization of the storage, residing on the CPU.
    Each serialized storage has a location tag associated with it which
    identifies the device it was saved from, and this tag is the second
    argument passed to :attr:`map_location`. The builtin location tags are ``'cpu'``
    for CPU tensors and ``'cuda:device_id'`` (e.g. ``'cuda:2'``) for CUDA tensors.
    :attr:`map_location` should return either ``None`` or a storage. If
    :attr:`map_location` returns a storage, it will be used as the final deserialized
    object, already moved to the right device. Otherwise, :func:`torch.load` will
    fall back to the default behavior, as if :attr:`map_location` wasn't specified.

    If :attr:`map_location` is a :class:`torch.device` object or a string containing
    a device tag, it indicates the location where all tensors should be loaded.

    Otherwise, if :attr:`map_location` is a dict, it will be used to remap location tags
    appearing in the file (keys), to ones that specify where to put the
    storages (values).

    User extensions can register their own location tags and tagging and
    deserialization methods using :func:`torch.serialization.register_package`.

    Args:
        f: a file-like object (has to implement :meth:`read`, :meth:`readline`, :meth:`tell`, and :meth:`seek`),
            or a string or os.PathLike object containing a file name
        map_location: a function, :class:`torch.device`, string or a dict specifying how to remap storage
            locations
        pickle_module: module used for unpickling metadata and objects (has to
            match the :attr:`pickle_module` used to serialize file)
        pickle_load_args: (Python 3 only) optional keyword arguments passed over to
            :func:`pickle_module.load` and :func:`pickle_module.Unpickler`, e.g.,
            :attr:`errors=...`.

    .. warning::
        :func:`torch.load()` uses ``pickle`` module implicitly, which is known to be insecure.
        It is possible to construct malicious pickle data which will execute arbitrary code
        during unpickling. Never load data that could have come from an untrusted
        source, or that could have been tampered with. **Only load data you trust**.

    .. note::
        When you call :func:`torch.load()` on a file which contains GPU tensors, those tensors
        will be loaded to GPU by default. You can call ``torch.load(.., map_location='cpu')``
        and then :meth:`load_state_dict` to avoid GPU RAM surge when loading a model checkpoint.

    .. note::
        By default, we decode byte strings as ``utf-8``.  This is to avoid a common error
        case ``UnicodeDecodeError: 'ascii' codec can't decode byte 0x...``
        when loading files saved by Python 2 in Python 3.  If this default
        is incorrect, you may use an extra :attr:`encoding` keyword argument to specify how
        these objects should be loaded, e.g., :attr:`encoding='latin1'` decodes them
        to strings using ``latin1`` encoding, and :attr:`encoding='bytes'` keeps them
        as byte arrays which can be decoded later with ``byte_array.decode(...)``.

    Example:
        >>> torch.load('tensors.pt')
        # Load all tensors onto the CPU
        >>> torch.load('tensors.pt', map_location=torch.device('cpu'))
        # Load all tensors onto the CPU, using a function
        >>> torch.load('tensors.pt', map_location=lambda storage, loc: storage)
        # Load all tensors onto GPU 1
        >>> torch.load('tensors.pt', map_location=lambda storage, loc: storage.cuda(1))
        # Map tensors from GPU 1 to GPU 0
        >>> torch.load('tensors.pt', map_location={'cuda:1':'cuda:0'})
        # Load tensor from io.BytesIO object
        >>> with open('tensor.pt', 'rb') as f:
        ...     buffer = io.BytesIO(f.read())
        >>> torch.load(buffer)
        # Load a module with 'ascii' encoding for unpickling
        >>> torch.load('module.pt', encoding='ascii')
    """
    _check_dill_version(pickle_module)

    if 'encoding' not in pickle_load_args.keys():
        pickle_load_args['encoding'] = 'utf-8'

    with _open_file_like(f, 'rb') as opened_file:
        if _is_zipfile(opened_file):
            # The zipfile reader is going to advance the current file position.
            # If we want to actually tail call to torch.jit.load, we need to
            # reset back to the original position.
            orig_position = opened_file.tell()
            with _open_zipfile_reader(opened_file) as opened_zipfile:
                if _is_torchscript_zip(opened_zipfile):
                    warnings.warn("'torch.load' received a zip file that looks like a TorchScript archive"
                                  " dispatching to 'torch.jit.load' (call 'torch.jit.load' directly to"
                                  " silence this warning)", UserWarning)
                    opened_file.seek(orig_position)
                    return torch.jit.load(opened_file)
                return _load(opened_zipfile, map_location, pickle_module, **pickle_load_args)
        return _legacy_load(opened_file, map_location, pickle_module, **pickle_load_args)


# Register pickling support for layout instances such as
# torch.sparse_coo, etc
def _get_layout(name):
    """Get layout extension object from its string representation.
    """
    cache = _get_layout.cache   # type: ignore[attr-defined]
    if not cache:
        for v in torch.__dict__.values():
            if isinstance(v, torch.layout):
                cache[str(v)] = v
    return cache[name]

# There are yet not good way to type annotate function attributes https://github.com/python/mypy/issues/2087
_get_layout.cache = {}   # type: ignore[attr-defined]
copyreg.pickle(torch.layout, lambda obj: (_get_layout, (str(obj),)))


def _legacy_load(f, map_location, pickle_module, **pickle_load_args):
    deserialized_objects: Dict[int, Any] = {}

    restore_location = _get_restore_location(map_location)

    class UnpicklerWrapper(pickle_module.Unpickler):  # type: ignore[name-defined]

        def find_class(self, mod_name, name):
            if type(name) is str and 'Storage' in name:
                try:
                    return StorageType(name)
                except KeyError:
                    pass
            return super().find_class(mod_name, name)

    def _check_container_source(container_type, source_file, original_source):
        try:
            current_source = ''.join(get_source_lines_and_file(container_type)[0])
        except Exception:  # saving the source is optional, so we can ignore any errors
            warnings.warn("Couldn't retrieve source code for container of "
                          "type " + container_type.__name__ + ". It won't be checked "
                          "for correctness upon loading.")
            return
        if original_source != current_source:
            if container_type.dump_patches:
                file_name = container_type.__name__ + '.patch'
                diff = difflib.unified_diff(current_source.split('\n'),
                                            original_source.split('\n'),
                                            source_file,
                                            source_file, lineterm="")
                lines = '\n'.join(diff)
                try:
                    with open(file_name, 'a+') as f:
                        file_size = f.seek(0, 2)
                        f.seek(0)
                        if file_size == 0:
                            f.write(lines)
                        elif file_size != len(lines) or f.read() != lines:
                            raise IOError
                    msg = ("Saved a reverse patch to " + file_name + ". "
                           "Run `patch -p0 < " + file_name + "` to revert your "
                           "changes.")
                except IOError:
                    msg = ("Tried to save a patch, but couldn't create a "
                           "writable file " + file_name + ". Make sure it "
                           "doesn't exist and your working directory is "
                           "writable.")
            else:
                msg = ("you can retrieve the original source code by "
                       "accessing the object's source attribute or set "
                       "`torch.nn.Module.dump_patches = True` and use the "
                       "patch tool to revert the changes.")
            msg = f"source code of class '{torch.typename(container_type)}' has changed. {msg}"
            warnings.warn(msg, SourceChangeWarning)

    def legacy_load(f):
        deserialized_objects: Dict[int, Any] = {}

        def persistent_load(saved_id):
            if isinstance(saved_id, tuple):
                # Ignore containers that don't have any sources saved
                if all(saved_id[1:]):
                    _check_container_source(*saved_id)
                return saved_id[0]
            return deserialized_objects[int(saved_id)]

        with closing(tarfile.open(fileobj=f, mode='r:', format=tarfile.PAX_FORMAT)) as tar, \
                mkdtemp() as tmpdir:

            tar.extract('storages', path=tmpdir)
            with open(os.path.join(tmpdir, 'storages'), 'rb', 0) as f:
                num_storages = pickle_module.load(f, **pickle_load_args)
                for i in range(num_storages):
                    args = pickle_module.load(f, **pickle_load_args)
                    key, location, storage_type = args
                    dtype = storage_type.dtype
                    obj = cast(Storage, torch._UntypedStorage)._new_with_file(f, torch._utils._element_size(dtype))
                    obj = restore_location(obj, location)
                    # TODO: Once we decide to break serialization FC, we can
                    # stop wrapping with _TypedStorage
                    deserialized_objects[key] = torch.storage._TypedStorage(
                        wrap_storage=obj,
                        dtype=dtype)

                storage_views = pickle_module.load(f, **pickle_load_args)
                for target_cdata, root_cdata, offset, numel in storage_views:
                    root = deserialized_objects[root_cdata]
                    element_size = torch._utils._element_size(root.dtype)
                    offset_bytes = offset * element_size
                    # TODO: Once we decide to break serialization FC, we can
                    # stop wrapping with _TypedStorage
                    deserialized_objects[target_cdata] = torch.storage._TypedStorage(
                        wrap_storage=root._storage[offset_bytes:offset_bytes + numel * element_size],
                        dtype=root.dtype)

            tar.extract('tensors', path=tmpdir)
            with open(os.path.join(tmpdir, 'tensors'), 'rb', 0) as f:
                num_tensors = pickle_module.load(f, **pickle_load_args)
                for _ in range(num_tensors):
                    args = pickle_module.load(f, **pickle_load_args)
                    key, storage_id, original_tensor_type = args
                    storage = deserialized_objects[storage_id]
                    ndim, = struct.unpack('<i', f.read(4))
                    # skip next 4 bytes; legacy encoding treated ndim as 8 bytes
                    f.read(4)
                    numel = struct.unpack(f'<{ndim}q', f.read(8 * ndim))
                    stride = struct.unpack(f'<{ndim}q', f.read(8 * ndim))
                    storage_offset, = struct.unpack('<q', f.read(8))
                    tensor = torch.tensor([], dtype=storage.dtype).set_(
                        storage._storage, storage_offset, numel, stride)
                    deserialized_objects[key] = tensor

            pickle_file = tar.extractfile('pickle')
            unpickler = UnpicklerWrapper(pickle_file, **pickle_load_args)
            unpickler.persistent_load = persistent_load
            result = unpickler.load()
            return result

    deserialized_objects = {}

    def persistent_load(saved_id):
        assert isinstance(saved_id, tuple)
        typename = _maybe_decode_ascii(saved_id[0])
        data = saved_id[1:]

        if typename == 'module':
            # Ignore containers that don't have any sources saved
            if all(data[1:]):
                _check_container_source(*data)
            return data[0]
        elif typename == 'storage':
            storage_type, root_key, location, numel, view_metadata = data
            location = _maybe_decode_ascii(location)
            dtype = storage_type.dtype

            nbytes = numel * torch._utils._element_size(dtype)

            if root_key not in deserialized_objects:
                obj = cast(Storage, torch._UntypedStorage(nbytes))
                obj._torch_load_uninitialized = True
                # TODO: Once we decide to break serialization FC, we can
                # stop wrapping with _TypedStorage
                deserialized_objects[root_key] = torch.storage._TypedStorage(
                    wrap_storage=restore_location(obj, location),
                    dtype=dtype)

            typed_storage = deserialized_objects[root_key]
            if view_metadata is not None:
                view_key, offset, view_size = view_metadata
                offset_bytes = offset * torch._utils._element_size(dtype)
                view_size_bytes = view_size * torch._utils._element_size(dtype)
                if view_key not in deserialized_objects:
                    # TODO: Once we decide to break serialization FC, we can
                    # stop wrapping with _TypedStorage
                    deserialized_objects[view_key] = torch.storage._TypedStorage(
                        wrap_storage=typed_storage._storage[offset_bytes:offset_bytes + view_size_bytes],
                        dtype=dtype)
                res = deserialized_objects[view_key]

            else:
                res = typed_storage
            return res
        else:
            raise RuntimeError("Unknown saved id type: %s" % saved_id[0])

    _check_seekable(f)
    f_should_read_directly = _should_read_directly(f)

    if f_should_read_directly and f.tell() == 0:
        # legacy_load requires that f has fileno()
        # only if offset is zero we can attempt the legacy tar file loader
        try:
            return legacy_load(f)
        except tarfile.TarError:
            if _is_zipfile(f):
                # .zip is used for torch.jit.save and will throw an un-pickling error here
                raise RuntimeError(
                    f"{f.name} is a zip archive (did you mean to use torch.jit.load()?)") from None
            # if not a tarfile, reset file offset and proceed
            f.seek(0)

    if not hasattr(f, 'readinto') and (3, 8, 0) <= sys.version_info < (3, 8, 2):
        raise RuntimeError(
            "torch.load does not work with file-like objects that do not implement readinto on Python 3.8.0 and 3.8.1. "
            f"Received object of type \"{type(f)}\". Please update to Python 3.8.2 or newer to restore this "
            "functionality.")

    magic_number = pickle_module.load(f, **pickle_load_args)
    if magic_number != MAGIC_NUMBER:
        raise RuntimeError("Invalid magic number; corrupt file?")
    protocol_version = pickle_module.load(f, **pickle_load_args)
    if protocol_version != PROTOCOL_VERSION:
        raise RuntimeError("Invalid protocol version: %s" % protocol_version)

    _sys_info = pickle_module.load(f, **pickle_load_args)
    unpickler = UnpicklerWrapper(f, **pickle_load_args)
    unpickler.persistent_load = persistent_load
    result = unpickler.load()

    deserialized_storage_keys = pickle_module.load(f, **pickle_load_args)

    offset = f.tell() if f_should_read_directly else None
    for key in deserialized_storage_keys:
        assert key in deserialized_objects
        typed_storage = deserialized_objects[key]
        typed_storage._storage._set_from_file(
            f, offset, f_should_read_directly,
            torch._utils._element_size(typed_storage.dtype))
        if offset is not None:
            offset = f.tell()

    torch._utils._validate_loaded_sparse_tensors()

    return result


def _maybe_decode_ascii(bytes_str: Union[bytes, str]) -> str:
    # When using encoding='bytes' in Py3, some **internal** keys stored as
    # strings in Py2 are loaded as bytes. This function decodes them with
    # ascii encoding, one that Py3 uses by default.
    #
    # NOTE: This should only be used on internal keys (e.g., `typename` and
    #       `location` in `persistent_load` below!
    if isinstance(bytes_str, bytes):
        return bytes_str.decode('ascii')
    return bytes_str


def _get_restore_location(map_location):
    if map_location is None:
        restore_location = default_restore_location
    elif isinstance(map_location, dict):
        def restore_location(storage, location):
            location = map_location.get(location, location)
            return default_restore_location(storage, location)
    elif isinstance(map_location, _string_classes):
        def restore_location(storage, location):
            return default_restore_location(storage, map_location)
    elif isinstance(map_location, torch.device):
        def restore_location(storage, location):
            return default_restore_location(storage, str(map_location))
    else:
        def restore_location(storage, location):
            result = map_location(storage, location)
            if result is None:
                result = default_restore_location(storage, location)
            return result
    return restore_location

class StorageType():
    def __init__(self, name):
        self.dtype = _get_dtype_from_pickle_storage_type(name)

    def __str__(self):
        return f'StorageType(dtype={self.dtype})'

def _load(zip_file, map_location, pickle_module, pickle_file='data.pkl', **pickle_load_args):
    restore_location = _get_restore_location(map_location)

    loaded_storages = {}

    def load_tensor(dtype, numel, key, location):
        name = f'data/{key}'

        storage = zip_file.get_storage_from_record(name, numel, torch._UntypedStorage).storage()._untyped()
        # TODO: Once we decide to break serialization FC, we can
        # stop wrapping with _TypedStorage
        loaded_storages[key] = torch.storage._TypedStorage(
            wrap_storage=restore_location(storage, location),
            dtype=dtype)

    def persistent_load(saved_id):
        assert isinstance(saved_id, tuple)
        typename = _maybe_decode_ascii(saved_id[0])
        data = saved_id[1:]

        assert typename == 'storage', \
            f"Unknown typename for persistent_load, expected 'storage' but got '{typename}'"
        storage_type, key, location, numel = data
        if storage_type is torch._UntypedStorage:
            dtype = torch.uint8
        else:
            dtype = storage_type.dtype

        if key not in loaded_storages:
            nbytes = numel * torch._utils._element_size(dtype)
            load_tensor(dtype, nbytes, key, _maybe_decode_ascii(location))

        return loaded_storages[key]

    load_module_mapping: Dict[str, str] = {
        # See https://github.com/pytorch/pytorch/pull/51633
        'torch.tensor': 'torch._tensor'
    }

    # Need to subclass Unpickler instead of directly monkey-patching the find_class method
    # because it's marked readonly in pickle.
    # The type: ignore is because mypy can't statically determine the type of this class.
    class UnpicklerWrapper(pickle_module.Unpickler):  # type: ignore[name-defined]
        # from https://stackoverflow.com/questions/13398462/unpickling-python-objects-with-a-changed-module-path/13405732
        # Lets us override the imports that pickle uses when unpickling an object.
        # This is useful for maintaining BC if we change a module path that tensor instantiation relies on.
        def find_class(self, mod_name, name):
            if type(name) is str and 'Storage' in name:
                try:
                    return StorageType(name)
                except KeyError:
                    pass
            mod_name = load_module_mapping.get(mod_name, mod_name)
            return super().find_class(mod_name, name)

    # Load the data (which may in turn use `persistent_load` to load tensors)
    data_file = io.BytesIO(zip_file.get_record(pickle_file))

    unpickler = UnpicklerWrapper(data_file, **pickle_load_args)
    unpickler.persistent_load = persistent_load
    result = unpickler.load()

    torch._utils._validate_loaded_sparse_tensors()

    return result


def _is_torchscript_zip(zip_file):
    return 'constants.pkl' in zip_file.get_all_records()
