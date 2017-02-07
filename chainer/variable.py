import copy
import collections
import heapq
import traceback
import warnings

import numpy
import six

import chainer
from chainer import cuda
from chainer import flag
from chainer import initializers


def _check_grad_type(func, x, gx):
    def make_message(message):
        if func:
            detail = 'Function `{0}` ({1}) has a bug.\n'.format(
                type(func).__name__, func.label)

            stack = func.stack
            if stack:
                detail += 'Stacktrace of the function is below:\n'
                for line in traceback.format_list(func._stack):
                    detail += line

            detail += '''
Please report this error to the issue tracker with the stack trace,
the information of your environment, and your script:
https://github.com/pfnet/chainer/issues/new.
'''.format(type(func).__name__, func.label)

        else:
            detail = ''

        detail += message
        return detail

    if not isinstance(gx, type(x._data)):
        msg = ('Type of data and grad mismatch\n%s != %s' %
               (type(x._data), type(gx)))
        raise TypeError(make_message(msg))
    if gx.dtype != x._data.dtype:
        msg = ('Dtype of data and grad mismatch\n%s != %s' %
               (x._data.dtype, gx.dtype))
        raise TypeError(make_message(msg))
    if gx.shape != x._data.shape:
        msg = ('Shape of data and grad mismatch\n%s != %s' %
               (x._data.shape, gx.shape))
        raise ValueError(make_message(msg))


def _vdata_to_cpu(vdata):
    # Friend function with _VariableData class.
    vdata1 = copy.copy(vdata)
    if vdata._data is None:
        vdata1._initial_device = 1
    else:
        vdata1._data = cuda.to_cpu(vdata._data)
        if vdata._grad is not None:
            vdata1._grad = cuda.to_cpu(vdata._grad)
    return vdata1


def _vdata_to_gpu(vdata, device=None):
    # Friend function with _VariableData class.
    vdata1 = copy.copy(vdata)
    if vdata._data is None:
        if device is None:
            vdata1._initial_device = cuda.Device().id
        else:
            vdata1._initial_device = device
    else:
        with cuda.get_device(device):
            vdata1._data = cuda.to_gpu(vdata._data)
            if vdata._grad is not None:
                vdata1._grad = cuda.to_gpu(vdata._grad)
    return vdata1


class _VariableData(object):

    _initializer = None
    _grad_initializer = None
    _initial_device = -1

    def __init__(self, data=None, grad=None, initializer=None):
        if data is None:
            if initializer is not None:
                self._initializer = initializer
            else:
                self._initializer = initializers.NaN()
            dtype = getattr(self.initializer, 'dtype', numpy.float32)
            self._grad_initializer = initializers.NaN(dtype)
        elif not isinstance(data, (numpy.ndarray, cuda.ndarray)):
                msg = '''numpy.ndarray or cuda.ndarray are expected.
Actual: {0}'''.format(type(data))
                raise TypeError(msg)

        self._data = data
        self._grad = grad

    @property
    def grad(self):
        return self._grad

    @grad.setter
    def grad(self, g):
        if g is not None:
            _check_grad_type(None, self, g)
        self._grad = g

    def cleargrad(self):
        self._grad = None
        if self._data is None:
            self._grad_initializer = None

    def zerograd(self):
        if self._data is None:
            dtype = getattr(self.initializer, 'dtype', None)
            self._grad_initializer = initializers.Zero(dtype)
            return

        with cuda.get_device(self._data) as dev:
            if self._grad is None:
                xp = numpy if int(dev) == -1 else cuda.cupy
                self._grad = xp.zeros_like(self._data)
            else:
                self._grad.fill(0)

    def addgrad(self, vdata):
        src = vdata._grad
        if src is None:
            return

        if self._data is None:
            self.initialize(vdata._data.shape)
        dst = self._grad

        src_dev = cuda.get_device(src)
        dst_dev = cuda.get_device(self._data)

        if src_dev.id == dst_dev.id:
            with dst_dev:
                if dst is None:
                    xp = cuda.get_array_module(src)
                    self._grad = xp.copy(src)
                else:
                    self._grad += src
            return

        if dst_dev.id < 0:
            src_grad = cuda.to_cpu(src)
        else:
            src_grad = cuda.to_gpu(src, device=dst_dev)

        if dst is None:
            self._grad = src_grad
        else:
            with dst_dev:
                self._grad += src_grad

    def initialize(self, shape):
        data = initializers.generate_array(self.initializer, shape, numpy)

        ginit = self._grad_initializer
        if ginit is None:
            grad = None
        else:
            grad = initializers.generator_array(ginit, shape, numpy)

        if self._initial_device >= 0:
            data = cuda.to_gpu(data, device=self._initial_device)
            if grad is not None:
                grad = cuda.to_gpu(grad, device=self._initial_device)

        self._data = data
        self._grad = grad


class Variable(object):

    """Array with a structure to keep track of computation.

    Every variable holds a data array of type either :class:`numpy.ndarray` or
    :class:`cupy.ndarray`.

    A Variable object may be constructed in two ways: by the user or by some
    function. When a variable is created by some function as one of its
    outputs, the variable holds a reference to that function. This reference is
    used in error backpropagation (a.k.a. backprop). It is also used in
    *backward unchaining*. A variable that does not hold a reference to its
    creator is called a *root* variable. A variable is root if it is created by
    the user, or if the reference is deleted by :meth:`unchain_backward`.

    Users can disable this chaining behavior by setting the volatile flag for
    the initial variables. When a function gets volatile variables as its
    inputs, the output variables do not hold references to the function. This
    acts like unchaining on every function application.

    Args:
        data (array): Initial data array.
        volatile (~chainer.Flag): Volatility flag. String ('on', 'off', or
            'auto') or boolean values can be used, too.
        name (str): Name of the variable.
        grad (array): Initial gradient array.
        initializer (~chainer.Initializer): Initializer of the data array.
            If `data` is None, this object is used for initializing the data
            array in the :meth:`initialize` method.

    Attributes:
        data: Data array of type either :class:`numpy.ndarray` or
            :class:`cupy.ndarray`. If it is None, the variable is left in an
            uninitialized state.
        grad: Gradient array.
        creator: The function who creates this variable. It is ``None`` if the
            variable is not created by any function.
        volatile: Ternary :class:`~chainer.Flag` object. If ``'ON'``, the
            variable does not keep track of any function applications. See
            :class:`~chainer.Flag` for the detail of ternary flags.
        initializer: Initializer of the data array. It is used for initializing
            the data array of an uninitialized variable.

    """

    def __init__(self, data=None, volatile=flag.OFF, name=None, grad=None,
                 initializer=None):
        self._vdata = _VariableData(data=data, grad=grad,
                                    initializer=initializer)
        self.rank = 0
        self._volatile = flag.Flag(volatile)
        self.creator = None
        self.name = name

    def __reduce__(self):
        return Variable, (self.data, self.volatile, self.name, self.grad)

    def __repr__(self):
        if self.name:
            return '<variable %s>' % self.name
        else:
            return '<variable at 0x%x>' % id(self)

    def __str__(self):
        return self.name or ('<var@%x>' % id(self))

    def debug_print(self):
        """Display a summary of the stored data and location of the Variable"""

        msg = """{summary}
- device: {device}
- volatile: {volatile}
- backend: {background}
- shape: {shape}
- dtype: {dtype}
- statistics: {stats}
- grad: {grad}"""

        stats_msg = 'mean={0:.8f}, std={1:.8f}'

        try:
            device = self.data.device
        except AttributeError:
            device = 'CPU'

        with cuda.get_device(self.data) as dev:
            xp = numpy if int(dev) == -1 else cuda.cupy

            if self.grad is None:
                grad = None
            elif xp.all(self.grad == 0):
                grad = 0
            else:
                grad = stats_msg.format(float(xp.mean(self.grad)),
                                        float(xp.std(self.grad)))

            stats = stats_msg.format(float(xp.mean(self.data)),
                                     float(xp.std(self.data)))

        return msg.format(summary=repr(self), volatile=self.volatile,
                          grad=grad, shape=self.data.shape,
                          background=type(self.data),
                          dtype=self.data.dtype, device=device,
                          stats=stats)

    def __pos__(self):
        return self

    def __len__(self):
        """Returns the number of elements of the data array.

        Returns:
            int: Number of elements of the data array.

        """
        return self.data.size

    @property
    def volatile(self):
        return self._volatile

    @volatile.setter
    def volatile(self, v):
        self._volatile = flag.Flag(v)

    @property
    def label(self):
        """Short text that represents the variable."""
        if self.data.shape == ():
            return str(self.data.dtype)
        return '(%s), %s' % (', '.join(map(str, self.data.shape)),
                             str(self.data.dtype))

    @property
    def data(self):
        return self._vdata._data

    @data.setter
    def data(self, d):
        self._vdata._data = d

    @property
    def grad(self):
        return self._vdata._grad

    @grad.setter
    def grad(self, g):
        self._vdata._grad = g

    @property
    def shape(self):
        return self.data.shape

    @property
    def ndim(self):
        return self.data.ndim

    @property
    def size(self):
        return self.data.size

    @property
    def dtype(self):
        return self.data.dtype

    def to_cpu(self):
        """Copies the data and gradient arrays to CPU."""
        self._vdata = _vdata_to_cpu(self._vdata)

    def to_gpu(self, device=None):
        """Copies the data and gradient arrays to specified GPU.

        Args:
            device: Target device specifier. If omitted, the current device is
                used.

        """
        self._vdata = _vdata_to_gpu(self._vdata, device=device)

    def cleargrad(self):
        """Clears the gradient array."""
        self._vdata.cleargrad()

    def zerograd(self):
        """Initializes the gradient array by zeros.

        .. deprecated:: v1.15
           Use :meth:`cleargrad` instead.

        """
        warnings.warn(
            'Variable.zerograd is deprecated. Use Variable.cleargard instead.',
            DeprecationWarning)
        self._vdata.zerograd()

    def copydata(self, var):
        """Copies the data array from given source variable.

        This method copies the data array from given variable to this variable.
        The copy is done even if the arrays reside on different devices,
        including across the host and a GPU device. If this variable has an
        uninitialized data array, this method initializes it by the data array
        of the given variable. Similarly, if the given variable has an
        uninitialized data array, this method initializes it by the data array
        of this variable (``self``). If both are uninitialized, this method
        does nothing.

        Args:
            var (Variable): Source variable.

        """
        src = var.data
        dst = self.data
        if src is None:
            if dst is None:
                return
            var.initialize(self.shape)
            src = var.data
        elif dst is None:
            self.initialize(src.shape)
            dst = self.data
        src_xp = cuda.get_array_module(src)
        dst_xp = cuda.get_array_module(dst)
        if dst_xp is src_xp:
            dst_xp.copyto(dst, src)
        elif dst_xp is numpy:
            dst_xp.copyto(dst, src.get())
        else:
            dst.set(src)

    def addgrad(self, var):
        """Accumulates the gradient array from given source variable.

        This method adds the gradient of a given variable to the gradient of
        this variable. The accumulation is even done across the host and
        different devices. If this variable has uninitialized data/grad arrays,
        this method initializes it with the shape of the given varaible and
        then accumulates the gradient.

        Args:
            var (Variable): Source variable.

        """
        self._vdata.addgrad(var._vdata)

    def set_creator(self, gen_func):
        """Notifies the variable that the given function is its creator.

        Args:
            gen_func (Function): Function object that creates this variable as
                one of its outputs.

        """
        self.creator = gen_func
        self.rank = gen_func.rank + 1

    def backward(self, retain_grad=False):
        """Runs error backpropagation (a.k.a. backprop) from this variable.

        On backprop, :meth:`Function.backward` is called on each
        :class:`Function` object appearing in the backward graph starting from
        this variable. The backward graph is represented by backward references
        from variables to their creators, and from functions to their inputs.
        The backprop stops at all root variables. Some functions set ``None``
        as gradients of some inputs, where further backprop does not take place
        at such input variables.

        This method uses :data:`grad` as the initial error array. User can
        manually set a gradient array before calling this method. If
        :data:`data` contains only one element (i.e., it is scalar) and
        :data:`grad` is ``None``, then this method automatically complements
        1.0 as the initial error. This is useful on starting backprop from
        some scalar loss value.

        Args:
            retain_grad (bool): If ``True``, the gradient arrays of all
                intermediate variables are kept. Otherwise, :data:`grad` of the
                intermediate variables are set to ``None`` on appropriate
                timing, which may reduce the maximum memory consumption.

                In most cases of training some models, the purpose of backprop
                is to compute gradients of parameters, not of variables, so it
                is recommended to set this flag ``False``.

        """
        if self.creator is None:
            return

        cand_funcs = []
        seen_set = set()
        seen_vars = set()
        need_copy = set()

        # Initialize error by 1, if this is a loss variable
        if self.data.size == 1 and self.grad is None:
            with cuda.get_device(self.data) as device:
                if device is cuda.DummyDevice:
                    self.grad = numpy.ones_like(self.data)
                else:
                    self.grad = cuda.cupy.ones_like(self.data)

        def add_cand(cand):
            if cand not in seen_set:
                # Negate since heapq is min-heap
                heapq.heappush(cand_funcs, (-cand.rank, len(seen_set), cand))
                seen_set.add(cand)

        add_cand(self.creator)

        while cand_funcs:
            _, _, func = heapq.heappop(cand_funcs)
            outputs = tuple(y() for y in func.outputs)  # access via weak ref

            in_data = tuple(x.data for x in func.inputs)
            out_grad = tuple(None if y is None else y.grad for y in outputs)
            hooks = chainer.get_function_hooks()
            if func._n_local_function_hooks != 0:
                hooks = collections.OrderedDict(hooks)
                hooks.update(func.local_function_hooks)
            for hook in six.itervalues(hooks):
                hook.backward_preprocess(func, in_data, out_grad)
            with cuda.get_device(*(in_data + out_grad)):
                gxs = func.backward(in_data, out_grad)
            assert len(gxs) == len(in_data)
            for hook in six.itervalues(hooks):
                hook.backward_postprocess(func, in_data, out_grad)

            if chainer.is_debug():
                if any(gx is not None and
                       cuda.get_array_module(gx).isnan(gx).any()
                       for gx in gxs):
                    msg = 'NaN is detected on backward computation'
                    raise RuntimeError(msg)

            if not retain_grad:
                for y in outputs:
                    if y is not None and y is not self:
                        y.grad = None
            for x, gx in zip(func.inputs, gxs):
                if gx is None:
                    continue

                _check_grad_type(func, x, gx)

                # Accumulate the gradient to x. It is a bit tricky to handle
                # branches and parameter gradient accumulation correctly.
                with cuda.get_device(gx):
                    id_x = id(x)
                    if x.creator is None:  # leaf
                        if x.grad is None:
                            x.grad = gx
                            need_copy.add(id_x)
                        elif id_x in need_copy:
                            x.grad = x.grad + gx  # copy
                            need_copy.remove(id_x)
                        else:
                            x.grad += gx
                    else:  # not a leaf
                        add_cand(x.creator)
                        if id_x not in seen_vars:  # 1st visit
                            x.grad = gx
                            seen_vars.add(id_x)
                            need_copy.add(id_x)
                        elif id_x in need_copy:  # 2nd visit
                            x.grad = gx + x.grad  # copied
                            need_copy.remove(id_x)
                        else:  # 3rd or later visit
                            x.grad += gx
            del gxs  # to reduce memory usage

    def unchain_backward(self):
        """Deletes references between variables and functions backward.

        After this method completes, intermediate variables and functions that
        are not referenced from anywhere are deallocated by reference
        count GC. Also this variable itself deletes the reference to its
        creator function, i.e. this variable becomes root in the computation
        graph. It indicates that backprop after unchaining stops at this
        variable. This behavior is useful to implement truncated BPTT.

        """
        cand_funcs = []
        seen_set = set()

        def add_cand(cand):
            if cand is not None and cand not in seen_set:
                cand_funcs.append(cand)
                seen_set.add(cand)

        add_cand(self.creator)

        while cand_funcs:
            func = cand_funcs.pop()
            for var in func.inputs:
                add_cand(var.creator)
            func.unchain()

    def initialize(self, shape):
        """Initializes the uninitialized variable.

        Uninitialized variable is a variable created with the data array set to
        None. This method creates and initializes the data array. The shape of
        the variable can be left unknown until this method is called.

        Args:
            shape (tuple of int): Shape of the data array.

        """
        self._vdata.initialize(shape)

    def __lt__(self, other):
        raise NotImplementedError()

    def __le__(self, other):
        raise NotImplementedError()

    def __eq__(self, other):
        raise NotImplementedError()

    def __ne__(self, other):
        raise NotImplementedError()

    def __gt__(self, other):
        raise NotImplementedError()

    def __ge__(self, other):
        raise NotImplementedError()

    def __nonzero__(self):
        raise NotImplementedError()

    def __bool__(self):
        raise NotImplementedError()

    def __hash__(self):
        return super(Variable, self).__hash__()

    __array_priority__ = 200
