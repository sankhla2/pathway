# Copyright © 2023 Pathway

from __future__ import annotations

import collections
import datetime
import typing
from abc import ABC, abstractmethod
from functools import cached_property
from types import EllipsisType, NoneType, UnionType
from warnings import warn

import numpy as np

from pathway.internals import api, datetime_types
from pathway.internals import json as js

if typing.TYPE_CHECKING:
    from pathway.internals.schema import Schema


class DType(ABC):
    _cache: dict[typing.Any, DType] = {}

    def to_engine(self) -> api.PathwayType | None:
        return None

    @abstractmethod
    def is_value_compatible(self, arg) -> bool:
        ...

    @abstractmethod
    def _set_args(self, *args):
        ...

    @classmethod
    def _cached_new(cls, *args):
        key = (cls, args)
        if key not in DType._cache:
            ret = super().__new__(cls)
            ret._set_args(*args)
            cls._cache[key] = ret
        return DType._cache[key]

    def __class_getitem__(cls, args):
        if isinstance(args, tuple):
            return cls(*args)
        else:
            return cls(args)  # type: ignore[call-arg]

    def equivalent_to(self, other: DType) -> bool:
        return dtype_equivalence(self, other)

    def is_subclass_of(self, other: DType) -> bool:
        return dtype_issubclass(self, other)

    @property
    @abstractmethod
    def typehint(self) -> typing.Any:
        ...


class _SimpleDType(DType):
    wrapped: type

    def __repr__(self):
        return {
            INT: "INT",
            BOOL: "BOOL",
            STR: "STR",
            FLOAT: "FLOAT",
        }[self]

    def _set_args(self, wrapped):
        self.wrapped = wrapped

    def __new__(cls, wrapped: type) -> _SimpleDType:
        return cls._cached_new(wrapped)

    def is_value_compatible(self, arg):
        if isinstance(arg, int) and self.wrapped == float:
            return True
        return isinstance(arg, self.wrapped)

    def to_engine(self) -> api.PathwayType:
        return {
            INT: api.PathwayType.INT,
            BOOL: api.PathwayType.BOOL,
            STR: api.PathwayType.STRING,
            FLOAT: api.PathwayType.FLOAT,
        }[self]

    @property
    def typehint(self) -> type:
        return self.wrapped


INT: DType = _SimpleDType(int)
BOOL: DType = _SimpleDType(bool)
STR: DType = _SimpleDType(str)
FLOAT: DType = _SimpleDType(float)


class _NoneDType(DType):
    def __repr__(self):
        return "NONE"

    def _set_args(self):
        pass

    def __new__(cls) -> _NoneDType:
        return cls._cached_new()

    def is_value_compatible(self, arg):
        return arg is None

    @property
    def typehint(self) -> None:
        return None


NONE: DType = _NoneDType()


class _AnyDType(DType):
    def __repr__(self):
        return "ANY"

    def _set_args(self):
        pass

    def to_engine(self) -> api.PathwayType:
        return api.PathwayType.ANY

    def __new__(cls) -> _AnyDType:
        return cls._cached_new()

    def is_value_compatible(self, arg):
        return True

    @property
    def typehint(self) -> typing.Any:
        return typing.Any


ANY: DType = _AnyDType()


class Callable(DType):
    arg_types: EllipsisType | tuple[DType, ...]
    return_type: DType

    def __repr__(self):
        if isinstance(self.arg_types, EllipsisType):
            return f"Callable(..., {self.return_type})"
        else:
            return f"Callable({self.arg_types}, {self.return_type})"

    def _set_args(self, arg_types, return_type):
        if isinstance(arg_types, EllipsisType):
            self.arg_types = ...
        else:
            self.arg_types = tuple(wrap(dtype) for dtype in arg_types)
        self.return_type = wrap(return_type)

    def __new__(
        cls,
        arg_types: EllipsisType | tuple[DType | EllipsisType, ...],
        return_type: DType,
    ) -> Callable:
        return cls._cached_new(arg_types, return_type)

    def is_value_compatible(self, arg):
        return callable(arg)

    @cached_property
    def typehint(self) -> typing.Any:
        if isinstance(self.arg_types, EllipsisType):
            return typing.Callable[..., self.return_type.typehint]
        else:
            return typing.Callable[
                [dtype.typehint for dtype in self.arg_types],
                self.return_type.typehint,
            ]


class Array(DType):
    def __repr__(self):
        return "Array"

    def _set_args(self):
        pass

    def to_engine(self) -> api.PathwayType:
        return api.PathwayType.ARRAY

    def __new__(cls) -> Array:
        return cls._cached_new()

    def is_value_compatible(self, arg):
        return isinstance(arg, np.ndarray)

    @property
    def typehint(self) -> type[np.ndarray]:
        return np.ndarray


ARRAY: DType = Array()

T = typing.TypeVar("T")


class Pointer(DType, typing.Generic[T]):
    wrapped: type[Schema] | None = None

    def __repr__(self):
        if self.wrapped is not None:
            return f"Pointer({self.wrapped.__name__})"
        else:
            return "Pointer"

    def _set_args(self, wrapped):
        self.wrapped = wrapped

    def to_engine(self) -> api.PathwayType:
        return api.PathwayType.POINTER

    def __new__(cls, wrapped: type[Schema] | None = None) -> Pointer:
        return cls._cached_new(wrapped)

    def is_value_compatible(self, arg):
        return isinstance(arg, api.BasePointer)

    @cached_property
    def typehint(self) -> type[api.Pointer]:
        if self.wrapped is None:
            return api.Pointer
        else:
            return api.Pointer[self.wrapped]  # type: ignore[name-defined]


POINTER: DType = Pointer()


class Optional(DType):
    wrapped: DType

    def __init__(self, arg):
        super().__init__()

    def __repr__(self):
        return f"Optional({self.wrapped})"

    def _set_args(self, wrapped):
        self.wrapped = wrapped

    def __new__(cls, arg: DType) -> DType:  # type:ignore[misc]
        arg = wrap(arg)
        if arg == NONE or isinstance(arg, Optional) or arg == ANY:
            return arg
        return cls._cached_new(arg)

    def is_value_compatible(self, arg):
        if arg is None:
            return True
        return self.wrapped.is_value_compatible(arg)

    @cached_property
    def typehint(self) -> type[UnionType]:
        return self.wrapped.typehint | None


class Tuple(DType):
    args: tuple[DType, ...]

    def __init__(self, *args):
        super().__init__()

    def __repr__(self):
        return f"Tuple({', '.join(str(arg) for arg in self.args)})"

    def _set_args(self, args):
        self.args = args

    def __new__(cls, *args: DType | EllipsisType) -> Tuple | List:  # type: ignore[misc]
        if any(isinstance(arg, EllipsisType) for arg in args):
            arg, placeholder = args
            assert isinstance(placeholder, EllipsisType)
            assert isinstance(arg, DType)
            return List(arg)
        else:
            return cls._cached_new(tuple(wrap(arg) for arg in args))

    def is_value_compatible(self, arg):
        if not isinstance(arg, (tuple, list)):
            return False
        elif len(self.args) != len(arg):
            return False
        else:
            return all(
                subdtype.is_value_compatible(subvalue)
                for subdtype, subvalue in zip(self.args, arg)
            )

    @cached_property
    def typehint(self) -> type[tuple]:
        return tuple[tuple(arg.typehint for arg in self.args)]  # type: ignore[misc]


class Json(DType):
    def __new__(cls) -> Json:
        return cls._cached_new()

    def _set_args(self):
        pass

    def to_engine(self) -> api.PathwayType:
        return api.PathwayType.JSON

    def __repr__(self) -> str:
        return "Json"

    def is_value_compatible(self, arg):
        return isinstance(arg, js.Json)

    @property
    def typehint(self) -> type[js.Json]:
        return js.Json


JSON: DType = Json()


class List(DType):
    wrapped: DType

    def __repr__(self):
        return f"List({self.wrapped})"

    def __new__(cls, wrapped: DType) -> List:
        return cls._cached_new(wrap(wrapped))

    def _set_args(self, wrapped):
        self.wrapped = wrapped

    def is_value_compatible(self, arg):
        return isinstance(arg, (tuple, list)) and all(
            self.wrapped.is_value_compatible(val) for val in arg
        )

    @cached_property
    def typehint(self) -> type[list]:
        return list[self.wrapped.typehint]  # type: ignore[name-defined]


ANY_TUPLE: DType = List._cached_new(
    ANY
)  # List(ANY) but this requires `wrap()` to exist


class _DateTimeNaive(DType):
    def __repr__(self):
        return "DATE_TIME_NAIVE"

    def _set_args(self):
        pass

    def __new__(cls) -> _DateTimeNaive:
        return cls._cached_new()

    def to_engine(self) -> api.PathwayType:
        return api.PathwayType.DATE_TIME_NAIVE

    def is_value_compatible(self, arg):
        return isinstance(arg, datetime.datetime) and arg.tzinfo is None

    @property
    def typehint(self) -> type[datetime_types.DateTimeNaive]:
        return datetime_types.DateTimeNaive


DATE_TIME_NAIVE = _DateTimeNaive()


class _DateTimeUtc(DType):
    def __repr__(self):
        return "DATE_TIME_UTC"

    def _set_args(self):
        pass

    def __new__(cls) -> _DateTimeUtc:
        return cls._cached_new()

    def to_engine(self) -> api.PathwayType:
        return api.PathwayType.DATE_TIME_UTC

    def is_value_compatible(self, arg):
        return isinstance(arg, datetime.datetime) and arg.tzinfo is not None

    @property
    def typehint(self) -> type[datetime_types.DateTimeUtc]:
        return datetime_types.DateTimeUtc


DATE_TIME_UTC = _DateTimeUtc()


class _Duration(DType):
    def __repr__(self):
        return "DURATION"

    def _set_args(self):
        pass

    def __new__(cls) -> _Duration:
        return cls._cached_new()

    def to_engine(self) -> api.PathwayType:
        return api.PathwayType.DURATION

    def is_value_compatible(self, arg):
        return isinstance(arg, datetime.timedelta)

    @property
    def typehint(self) -> type[datetime_types.Duration]:
        return datetime_types.Duration


DURATION = _Duration()


def wrap(input_type) -> DType:
    assert input_type != ...
    assert input_type != Optional
    assert input_type != Pointer
    assert input_type != Tuple
    assert input_type != Callable
    assert input_type != Array
    assert input_type != List
    assert input_type != Json
    if isinstance(input_type, DType):
        return input_type
    if input_type in (NoneType, None):
        return NONE
    elif input_type == typing.Any:
        return ANY
    elif (
        input_type is api.BasePointer
        or input_type is api.Pointer
        or typing.get_origin(input_type) is api.Pointer
    ):
        args = typing.get_args(input_type)
        if len(args) == 0:
            return Pointer()
        else:
            assert len(args) == 1
            return Pointer(args[0])
    elif isinstance(input_type, str):
        return ANY  # TODO: input_type is annotation for class
    elif typing.get_origin(input_type) == collections.abc.Callable:
        c_args = get_args(input_type)
        if c_args == ():
            return Callable(..., ANY)
        arg_types, ret_type = c_args
        if isinstance(arg_types, Tuple):
            callable_args: tuple[DType, ...] | EllipsisType = arg_types.args
        else:
            assert isinstance(arg_types, EllipsisType)
            callable_args = arg_types
        assert isinstance(ret_type, DType), type(ret_type)
        return Callable(callable_args, ret_type)
    elif (
        typing.get_origin(input_type) in (typing.Union, UnionType)
        and len(typing.get_args(input_type)) == 2
        and isinstance(None, typing.get_args(input_type)[1])
    ):
        arg, _ = get_args(input_type)
        assert isinstance(arg, DType)
        return Optional(arg)
    elif input_type in [list, tuple, typing.List, typing.Tuple]:
        return ANY_TUPLE
    elif (
        input_type == js.Json
        or input_type == dict
        or typing.get_origin(input_type) == dict
    ):
        return JSON
    elif typing.get_origin(input_type) == list:
        args = get_args(input_type)
        (arg,) = args
        return List(wrap(arg))
    elif typing.get_origin(input_type) == tuple:
        args = get_args(input_type)
        if args[-1] == ...:
            arg, _ = args
            return List(wrap(arg))
        else:
            return Tuple(*[wrap(arg) for arg in args])
    elif input_type == np.ndarray:
        return ARRAY
    else:
        dtype = {
            int: INT,
            bool: BOOL,
            str: STR,
            float: FLOAT,
            datetime_types.Duration: DURATION,
            datetime_types.DateTimeNaive: DATE_TIME_NAIVE,
            datetime_types.DateTimeUtc: DATE_TIME_UTC,
        }.get(input_type, ANY)
        if dtype == ANY:
            # TODO ideally below line would be uncommented
            # raise TypeError(f"Unsupported type {input_type}.")
            warn(f"Unsupported type {input_type}, falling back to ANY.")
        return dtype


def dtype_equivalence(left: DType, right: DType) -> bool:
    return dtype_issubclass(left, right) and dtype_issubclass(right, left)


def dtype_tuple_equivalence(left: Tuple | List, right: Tuple | List) -> bool:
    if left == ANY_TUPLE or right == ANY_TUPLE:
        return True
    if isinstance(left, List) and isinstance(right, List):
        return left.wrapped == right.wrapped
    if isinstance(left, List):
        assert isinstance(right, Tuple)
        assert not isinstance(right.args, EllipsisType)
        rargs = right.args
        largs = tuple(left.wrapped for _arg in rargs)
    elif isinstance(right, List):
        assert isinstance(left, Tuple)
        assert not isinstance(left.args, EllipsisType)
        largs = left.args
        rargs = tuple(right.wrapped for _arg in largs)
    else:
        assert isinstance(left, Tuple)
        assert isinstance(right, Tuple)
        assert not isinstance(left.args, EllipsisType)
        assert not isinstance(right.args, EllipsisType)
        largs = left.args
        rargs = right.args
    if len(largs) != len(rargs):
        return False
    return all(dtype_equivalence(l_arg, r_arg) for l_arg, r_arg in zip(largs, rargs))


def dtype_issubclass(left: DType, right: DType) -> bool:
    if right == ANY:  # catch the case, when left=Optional[T] and right=Any
        return True
    elif isinstance(left, Optional):
        if isinstance(right, Optional):
            return dtype_issubclass(unoptionalize(left), unoptionalize(right))
        else:
            return False
    elif left == NONE:
        return isinstance(right, Optional) or right == NONE
    elif isinstance(right, Optional):
        return dtype_issubclass(left, unoptionalize(right))
    elif isinstance(left, (Tuple, List)) and isinstance(right, (Tuple, List)):
        return dtype_tuple_equivalence(left, right)
    elif isinstance(left, Pointer) and isinstance(right, Pointer):
        return True  # TODO
    elif isinstance(left, _SimpleDType) and isinstance(right, _SimpleDType):
        if left == INT and right == FLOAT:
            return True
        elif left == BOOL and right == INT:
            return False
        else:
            return issubclass(left.wrapped, right.wrapped)
    elif isinstance(left, Callable) and isinstance(right, Callable):
        return True
    return left == right


def types_lca(left: DType, right: DType) -> DType:
    """LCA of two types."""
    if isinstance(left, Optional) or isinstance(right, Optional):
        return Optional(types_lca(unoptionalize(left), unoptionalize(right)))
    elif isinstance(left, (Tuple, List)) and isinstance(right, (Tuple, List)):
        if left == ANY_TUPLE or right == ANY_TUPLE:
            return ANY_TUPLE
        elif dtype_tuple_equivalence(left, right):
            return left
        else:
            return ANY_TUPLE
    elif isinstance(left, Pointer) and isinstance(right, Pointer):
        l_schema = left.wrapped
        r_schema = right.wrapped
        if l_schema is None:
            return right
        elif r_schema is None:
            return left
        if l_schema == r_schema:
            return left
        else:
            return POINTER
    if dtype_issubclass(left, right):
        return right
    elif dtype_issubclass(right, left):
        return left

    if left == NONE:
        return Optional(right)
    elif right == NONE:
        return Optional(left)
    else:
        return ANY


def unoptionalize(dtype: DType) -> DType:
    return dtype.wrapped if isinstance(dtype, Optional) else dtype


def normalize_dtype(dtype: DType) -> DType:
    if isinstance(dtype, Pointer):
        return POINTER
    if isinstance(dtype, Array):
        return ARRAY
    return dtype


def unoptionalize_pair(left_dtype: DType, right_dtype) -> tuple[DType, DType]:
    """
    Unpacks type out of typing.Optional and matches
    a second type with it if it is an EmptyType.
    """
    if left_dtype == NONE and isinstance(right_dtype, Optional):
        left_dtype = right_dtype
    if right_dtype == NONE and isinstance(left_dtype, Optional):
        right_dtype = left_dtype

    return unoptionalize(left_dtype), unoptionalize(right_dtype)


def get_args(dtype: typing.Any) -> tuple[EllipsisType | DType, ...]:
    arg_types = typing.get_args(dtype)
    return tuple(wrap(arg) if arg != ... else ... for arg in arg_types)
