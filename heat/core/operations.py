import builtins
import numpy as np
import torch
import warnings

from .communication import MPI, MPI_WORLD
from . import factories
from . import stride_tricks
from . import dndarray
from . import types

__all__ = []


def __binary_op(operation, t1, t2):
    """
    Generic wrapper for element-wise binary operations of two operands (either can be tensor or scalar).
    Takes the operation function and the two operands involved in the operation as arguments.

    Parameters
    ----------
    operation : function
        The operation to be performed. Function that performs operation elements-wise on the involved tensors,
        e.g. add values from other to self

    t1: dndarray or scalar
        The first operand involved in the operation,

    t2: tensor or scalar
        The second operand involved in the operation,

    Returns
    -------
    result: ht.Tensor
        A tensor containing the results of element-wise operation.
    """

    if np.isscalar(t1):
        try:
            t1 = factories.array([t1])
        except (ValueError, TypeError,):
            raise TypeError('Data type not supported, input was {}'.format(type(t1)))

        if np.isscalar(t2):
            try:
                t2 = factories.array([t2])
            except (ValueError, TypeError,):
                raise TypeError('Only numeric scalars are supported, but input was {}'.format(type(t2)))
            output_shape = (1,)
            output_split = None
            output_device = None
            output_comm = MPI_WORLD
        elif isinstance(t2, dndarray.DNDarray):
            output_shape = t2.shape
            output_split = t2.split
            output_device = t2.device
            output_comm = t2.comm
        else:
            raise TypeError('Only tensors and numeric scalars are supported, but input was {}'.format(type(t2)))

        if t1.dtype != t2.dtype:
            t1 = t1.astype(t2.dtype)

    elif isinstance(t1, dndarray.DNDarray):
        if np.isscalar(t2):
            try:
                t2 = factories.array([t2])
                output_shape = t1.shape
                output_split = t1.split
                output_device = t1.device
                output_comm = t1.comm
            except (ValueError, TypeError,):
                raise TypeError('Data type not supported, input was {}'.format(type(t2)))

        elif isinstance(t2, dndarray.DNDarray):
            # TODO: implement complex NUMPY rules
            if t2.split is None or t2.split == t1.split:
                output_shape = stride_tricks.broadcast_shape(t1.shape, t2.shape)
                output_split = t1.split
                output_device = t1.device
                output_comm = t1.comm
            else:
                # It is NOT possible to perform binary operations on tensors with different splits, e.g. split=0
                # and split=1
                raise NotImplementedError('Not implemented for other splittings')

            # ToDo: Fine tuning in case of comm.size>t1.shape[t1.split]. Send torch tensors only to ranks, that will hold data.
            if t1.split is not None:
                if t1.shape[t1.split] == 1 and t1.comm.is_distributed():
                    warnings.warn('Broadcasting requires transferring data of first operator between MPI ranks!')
                    if t1.comm.rank > 0:
                        t1._DNDarray__array = torch.zeros(t1.shape, dtype=t1.dtype.torch_type())
                    t1.comm.Bcast(t1)

            if t2.split is not None:
                if t2.shape[t2.split] == 1 and t2.comm.is_distributed():
                    warnings.warn('Broadcasting requires transferring data of second operator between MPI ranks!')
                    if t2.comm.rank > 0:
                        t2._DNDarray__array = torch.zeros(t2.shape, dtype=t2.dtype.torch_type())
                    t2.comm.Bcast(t2)

        else:
            raise TypeError('Only tensors and numeric scalars are supported, but input was {}'.format(type(t2)))

        if t2.dtype != t1.dtype:
            t2 = t2.astype(t1.dtype)

    else:
        raise NotImplementedError('Not implemented for non scalar')

    promoted_type = types.promote_types(t1.dtype, t2.dtype).torch_type()
    if t1.split is not None:
        if t1.lshape[t1.split] == 0:
            result = t1._DNDarray__array.type(promoted_type)
        else:
            result = operation(t1._DNDarray__array.type(promoted_type), t2._DNDarray__array.type(promoted_type))
    elif t1.split is not None:
        if t2.lshape[t2.split] == 0:
            result = t2._DNDarray__array.type(promoted_type)
        else:
            result = operation(t1._DNDarray__array.type(promoted_type), t2._DNDarray__array.type(promoted_type))
    else:
        result = operation(t1._DNDarray__array.type(promoted_type), t2._DNDarray__array.type(promoted_type))

    return dndarray.DNDarray(result, output_shape, types.canonical_heat_type(t1.dtype), output_split, output_device, output_comm)


def __local_op(operation, x, out):
    """
    Generic wrapper for local operations, which do not require communication. Accepts the actual operation function as
    argument and takes only care of buffer allocation/writing.

    Parameters
    ----------
    operation : function
        A function implementing the element-wise local operation, e.g. torch.sqrt
    x : ht.DNDarray
        The value for which to compute 'operation'.
    out : ht.DNDarray or None
        A location in which to store the results. If provided, it must have a broadcastable shape. If not provided or
        set to None, a fresh tensor is allocated.

    Returns
    -------
    result : ht.DNDarray
        A tensor of the same shape as x, containing the result of 'operation' for each element in x. If out was
        provided, result is a reference to it.

    Raises
    -------
    TypeError
        If the input is not a tensor or the output is not a tensor or None.
    """
    # perform sanitation
    if not isinstance(x, dndarray.DNDarray):
        raise TypeError('expected x to be a ht.DNDarray, but was {}'.format(type(x)))
    if out is not None and not isinstance(out, dndarray.DNDarray):
        raise TypeError('expected out to be None or an ht.DNDarray, but was {}'.format(type(out)))

    # infer the output type of the tensor
    # we need floating point numbers here, due to PyTorch only providing sqrt() implementation for float32/64
    promoted_type = types.promote_types(x.dtype, types.float32)
    torch_type = promoted_type.torch_type()

    # no defined output tensor, return a freshly created one
    if out is None:
        result = operation(x._DNDarray__array.type(torch_type))
        return dndarray.DNDarray(result, x.gshape, promoted_type, x.split, x.device, x.comm)

    # output buffer writing requires a bit more work
    # we need to determine whether the operands are broadcastable and the multiple of the broadcasting
    # reason: manually repetition for each dimension as PyTorch does not conform to numpy's broadcast semantic
    # PyTorch always recreates the input shape and ignores broadcasting for too large buffers
    broadcast_shape = stride_tricks.broadcast_shape(x.lshape, out.lshape)
    padded_shape = (1,) * (len(broadcast_shape) - len(x.lshape)) + x.lshape
    multiples = [int(a / b) for a, b in zip(broadcast_shape, padded_shape)]
    needs_repetition = builtins.any(multiple > 1 for multiple in multiples)

    # do an inplace operation into a provided buffer
    casted = x._DNDarray__array.type(torch_type)
    operation(casted.repeat(multiples) if needs_repetition else casted, out=out._DNDarray__array)

    return out


def __reduce_op(x, partial_op, reduction_op, **kwargs):
    # TODO: document me Issue #102
    # perform sanitation
    if not isinstance(x, dndarray.DNDarray):
        raise TypeError('expected x to be a ht.DNDarray, but was {}'.format(type(x)))
    out = kwargs.get('out')
    if out is not None and not isinstance(out, dndarray.DNDarray):
        raise TypeError('expected out to be None or an ht.DNDarray, but was {}'.format(type(out)))

    # no further checking needed, sanitize axis will raise the proper exceptions
    axis = stride_tricks.sanitize_axis(x.shape,  kwargs.get('axis'))
    split = x.split

    if axis is None:
        partial = partial_op(x._DNDarray__array).reshape(-1)
        output_shape = (1,)
    else:
        if isinstance(axis, int):
            axis = (axis,)

        if isinstance(axis, tuple):
            partial = x._DNDarray__array
            for dim in axis:
                partial = partial_op(partial, dim=dim, keepdim=True)
                shape_keepdim = x.gshape[:dim] + (1,) + x.gshape[dim + 1:]
            shape_losedim = tuple(x.gshape[dim] for dim in range(len(x.gshape)) if not dim in axis)

        output_shape = shape_keepdim if kwargs.get('keepdim') else shape_losedim

    # Check shape of output buffer, if any
    if out is not None and out.shape != output_shape:
        raise ValueError('Expecting output buffer of shape {}, got {}'.format(output_shape, out.shape))

    # perform a reduction operation in case the tensor is distributed across the reduction axis
    if x.split is not None and (axis is None or (x.split in axis)):
        split = None
        if x.comm.is_distributed():
            x.comm.Allreduce(MPI.IN_PLACE, partial, reduction_op)

    # if reduction_op is a Boolean operation, then resulting tensor is bool
    boolean_ops = [MPI.LAND, MPI.LOR, MPI.BAND, MPI.BOR]
    tensor_type = bool if reduction_op in boolean_ops else partial[0].dtype

    if out is not None:
        out._DNDarray__array = partial
        out._DNDarray__dtype = types.canonical_heat_type(tensor_type)
        out._DNDarray__split = split
        out._DNDarray__device = x.device
        out._DNDarray__comm = x.comm

        return out

    return dndarray.DNDarray(
        partial,
        output_shape,
        types.canonical_heat_type(tensor_type),
        split=split,
        device=x.device,
        comm=x.comm
    )
