# Copyright © 2023 Pathway

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from itertools import chain
from types import EllipsisType
from typing import TYPE_CHECKING, ClassVar

import pathway.internals as pw
from pathway.internals import column_properties as cp
from pathway.internals import dtype as dt
from pathway.internals import trace
from pathway.internals.dtype import DType
from pathway.internals.expression import ColumnExpression, ColumnReference
from pathway.internals.helpers import SetOnceProperty, StableSet

if TYPE_CHECKING:
    from pathway.internals.expression import InternalColRef
    from pathway.internals.operator import OutputHandle
    from pathway.internals.table import Table
    from pathway.internals.universe import Universe


@dataclass(eq=False, frozen=True)
class Lineage:
    source: OutputHandle
    """Source handle."""

    @property
    def trace(self) -> trace.Trace:
        return self.source.operator.trace


@dataclass(eq=False, frozen=True)
class ColumnLineage(Lineage):
    name: str
    """Original name of a column."""

    def get_original_column(self):
        if self.name == "id":
            return self.table._id_column
        else:
            return self.table._get_column(self.name)

    @property
    def is_method(self) -> bool:
        return isinstance(self.get_original_column(), MethodColumn)

    @property
    def table(self) -> Table:
        return self.source.value


class Column(ABC):
    universe: Universe
    lineage: SetOnceProperty[ColumnLineage] = SetOnceProperty()
    """Lateinit by operator."""

    def __init__(self, universe: Universe) -> None:
        super().__init__()
        self.universe = universe
        self._trace = trace.Trace.from_traceback()

    def column_dependencies(self) -> StableSet[Column]:
        return StableSet([self])

    @property
    def trace(self) -> trace.Trace:
        if hasattr(self, "lineage"):
            return self.lineage.trace
        else:
            return self._trace

    @property
    @abstractmethod
    def properties(self) -> cp.ColumnProperties:
        ...

    @property
    def dtype(self) -> DType:
        return self.properties.dtype


class MaterializedColumn(Column):
    """Column not requiring evaluation."""

    def __init__(
        self,
        universe: Universe,
        properties: cp.ColumnProperties,
    ):
        super().__init__(universe)
        self._properties = properties

    @property
    def properties(self) -> cp.ColumnProperties:
        return self._properties


class ExternalMaterializedColumn(MaterializedColumn):
    """Temporary construct to differentiate between internal and external
    MaterializedColumns in pw.iterate. Replace with MaterializedColumn when done"""

    # TODO
    pass


class MethodColumn(MaterializedColumn):
    """Column representing an output method in a RowTransformer."""

    pass


class ColumnWithContext(Column, ABC):
    """Column holding a context."""

    context: Context

    def __init__(self, context: Context, universe: Universe):
        super().__init__(universe)
        self.context = context

    def column_dependencies(self) -> StableSet[Column]:
        return super().column_dependencies() | self.context.column_dependencies()

    @cached_property
    def properties(self) -> cp.ColumnProperties:
        return self.context.column_properties(self)

    @cached_property
    @abstractmethod
    def context_dtype(self) -> DType:
        ...


class IdColumn(ColumnWithContext):
    def __init__(self, context: Context) -> None:
        super().__init__(context, context.universe)

    @cached_property
    def context_dtype(self) -> DType:
        return dt.POINTER


class ColumnWithExpression(ColumnWithContext):
    """Column holding expression and context."""

    expression: ColumnExpression
    context: Context

    def __init__(
        self,
        context: Context,
        universe: Universe,
        expression: ColumnExpression,
        lineage: Lineage | None = None,
    ):
        super().__init__(context, universe)
        self.expression = expression
        if lineage is not None:
            self.lineage = lineage

    def dereference(self) -> Column:
        raise RuntimeError("expression cannot be dereferenced")

    def column_dependencies(self) -> StableSet[Column]:
        return super().column_dependencies() | self.expression._column_dependencies()

    @cached_property
    def context_dtype(self) -> DType:
        return self.context.expression_type(self.expression)


class ColumnWithReference(ColumnWithExpression):
    expression: ColumnReference

    def __init__(
        self,
        context: Context,
        universe: Universe,
        expression: ColumnReference,
        lineage: Lineage | None = None,
    ):
        super().__init__(context, universe, expression, lineage)
        self.expression = expression
        if lineage is None:
            lineage = expression._column.lineage
            if lineage.is_method or universe == expression._column.universe:
                self.lineage = lineage

    def dereference(self) -> Column:
        return self.expression._column

    def column_dependencies(self) -> StableSet[Column]:
        return super().column_dependencies() | self.reference_column_dependencies()

    def reference_column_dependencies(self) -> StableSet[Column]:
        return self.context.reference_column_dependencies(self.expression)


@dataclass(eq=True, frozen=True)
class ContextTable:
    """Simplified table representation used in contexts."""

    columns: tuple[Column, ...]
    universe: Universe

    def __post_init__(self):
        assert all((column.universe == self.universe) for column in self.columns)


def _create_internal_table(
    columns: Iterable[Column], universe: Universe, context: Context
) -> Table:
    from pathway.internals.table import Table

    columns_dict = {f"{i}": column for i, column in enumerate(columns)}
    return Table(columns_dict, universe, id_column=IdColumn(context))


@dataclass(eq=False, frozen=True)
class Context:
    """Context of the column evaluation.

    Context will be mapped to proper evaluator based on its type.
    """

    universe: Universe
    """Resulting universe."""
    _column_properties_evaluator: ClassVar[type[cp.ColumnPropertiesEvaluator]]

    def column_dependencies_external(self) -> Iterable[Column]:
        return []

    def column_dependencies_internal(self) -> Iterable[Column]:
        return []

    def column_dependencies(self) -> StableSet[Column]:
        # columns depend on columns in their context, not dependencies of columns in context
        return StableSet(
            chain(
                self.column_dependencies_external(), self.column_dependencies_internal()
            )
        )

    def reference_column_dependencies(self, ref: ColumnReference) -> StableSet[Column]:
        return StableSet()

    def universe_dependencies(self) -> Iterable[Universe]:
        return []

    def _get_type_interpreter(self):
        from pathway.internals.type_interpreter import TypeInterpreter

        return TypeInterpreter()

    def expression_type(self, expression: ColumnExpression) -> dt.DType:
        return self.expression_with_type(expression)._dtype

    def expression_with_type(self, expression: ColumnExpression) -> ColumnExpression:
        from pathway.internals.type_interpreter import TypeInterpreterState

        return self._get_type_interpreter().eval_expression(
            expression, state=TypeInterpreterState()
        )

    def intermediate_tables(self) -> Iterable[Table]:
        dependencies = list(self.column_dependencies_internal())
        if len(dependencies) == 0:
            return []
        universe = None
        context = None
        columns: list[ColumnWithContext] = []
        for column in dependencies:
            assert isinstance(
                column, ColumnWithContext
            ), f"Column {column} that is not ColumnWithContext appeared in column_dependencies_internal()"
            assert universe is None or universe == column.universe
            assert context is None or context == column.context
            columns.append(column)
            universe = column.universe
            context = column.context
        assert universe is not None
        assert context is not None
        return [_create_internal_table(columns, universe, context)]

    def column_properties(self, column: ColumnWithContext) -> cp.ColumnProperties:
        return self._column_properties_evaluator().eval(column)

    def __init_subclass__(
        cls,
        /,
        column_properties_evaluator: type[
            cp.ColumnPropertiesEvaluator
        ] = cp.DefaultPropsEvaluator,
        **kwargs,
    ) -> None:
        super().__init_subclass__(**kwargs)
        cls._column_properties_evaluator = column_properties_evaluator


@dataclass(eq=False, frozen=True)
class RowwiseContext(
    Context, column_properties_evaluator=cp.PreserveDependenciesPropsEvaluator
):
    """Context for basic expressions."""

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.universe]


@dataclass(eq=False, frozen=True)
class TableRestrictedRowwiseContext(RowwiseContext):
    """Restricts expression to specific table."""

    table: pw.Table


@dataclass(eq=False, frozen=True)
class CopyContext(Context):
    """Context used by operators not changing the columns."""


@dataclass(eq=False, frozen=True)
class GroupedContext(Context):
    """Context of `table.groupby().reduce() operation."""

    table: pw.Table
    grouping_columns: dict[InternalColRef, Column]
    set_id: bool
    """Whether id should be set based on grouping column."""
    inner_context: RowwiseContext
    """Original context of grouped table."""
    requested_grouping_columns: StableSet[InternalColRef] = field(
        default_factory=StableSet, compare=False, hash=False
    )
    sort_by: InternalColRef | None = None

    def column_dependencies_internal(self) -> Iterable[Column]:
        return list(self.grouping_columns.values())

    def column_dependencies_external(self) -> Iterable[Column]:
        if self.sort_by is not None:
            return [self.sort_by.to_colref()._column]
        else:
            return []

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.inner_context.universe]


@dataclass(eq=False, frozen=True)
class FilterContext(
    Context, column_properties_evaluator=cp.PreserveDependenciesPropsEvaluator
):
    """Context of `table.filter() operation."""

    filtering_column: ColumnWithExpression
    universe_to_filter: Universe

    def column_dependencies_internal(self) -> Iterable[Column]:
        return [self.filtering_column]

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.universe_to_filter]


@dataclass(eq=False, frozen=True)
class TimeColumnContext(Context):
    """Context of operations that use time columns."""

    old_universe: Universe
    threshold_column: ColumnWithExpression
    time_column: ColumnWithExpression

    def column_dependencies_internal(self) -> Iterable[Column]:
        return [self.threshold_column, self.time_column]

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.old_universe]


@dataclass(eq=False, frozen=True)
class ForgetContext(TimeColumnContext):
    """Context of `table.forget() operation."""


@dataclass(eq=False, frozen=True)
class FreezeContext(TimeColumnContext):
    """Context of `table.freeze() operation."""


@dataclass(eq=False, frozen=True)
class BufferContext(TimeColumnContext):
    """Context of `table.buffer() operation."""


@dataclass(eq=False, frozen=True)
class ReindexContext(Context):
    """Context of `table.with_id() operation."""

    reindex_column: ColumnWithExpression

    def column_dependencies_internal(self) -> Iterable[Column]:
        return [self.reindex_column]

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.reindex_column.universe]


@dataclass(eq=False, frozen=True)
class IxContext(Context):
    """Context of `table.ix() operation."""

    orig_universe: Universe
    key_column: Column
    optional: bool

    def column_dependencies_external(self) -> Iterable[Column]:
        return [self.key_column]

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.universe, self.orig_universe]


@dataclass(eq=False, frozen=True)
class IntersectContext(Context):
    """Context of `table.intersect() operation."""

    intersecting_universes: tuple[Universe, ...]

    def __post_init__(self):
        assert len(self.intersecting_universes) > 0

    def universe_dependencies(self) -> Iterable[Universe]:
        return self.intersecting_universes


@dataclass(eq=False, frozen=True)
class RestrictContext(Context):
    """Context of `table.restrict() operation."""

    orig_universe: Universe

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.orig_universe, self.universe]


@dataclass(eq=False, frozen=True)
class DifferenceContext(Context):
    """Context of `table.difference() operation."""

    left: Universe
    right: Universe

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.left, self.right]


@dataclass(eq=False, frozen=True)
class HavingContext(Context):
    orig_universe: Universe
    key_column: Column

    def column_dependencies_external(self) -> Iterable[Column]:
        return [self.key_column]

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.orig_universe]


@dataclass(eq=False, frozen=True)
class UpdateRowsContext(Context):
    """Context of `table.update_rows()` and related operations."""

    updates: dict[str, Column]
    union_universes: tuple[Universe, ...]

    def __post_init__(self):
        assert len(self.union_universes) > 0

    def reference_column_dependencies(self, ref: ColumnReference) -> StableSet[Column]:
        return StableSet([self.updates[ref.name]])

    def universe_dependencies(self) -> Iterable[Universe]:
        return self.union_universes


@dataclass(eq=False, frozen=True)
class UpdateCellsContext(UpdateRowsContext):
    def reference_column_dependencies(self, ref: ColumnReference) -> StableSet[Column]:
        if ref.name in self.updates:
            return super().reference_column_dependencies(ref)
        return StableSet()


@dataclass(eq=False, frozen=True)
class ConcatUnsafeContext(Context):
    """Context of `table.concat_unsafe()`."""

    updates: tuple[dict[str, Column], ...]
    union_universes: tuple[Universe, ...]

    def __post_init__(self):
        assert len(self.union_universes) > 0

    def reference_column_dependencies(self, ref: ColumnReference) -> StableSet[Column]:
        return StableSet([update[ref.name] for update in self.updates])

    def universe_dependencies(self) -> Iterable[Universe]:
        return self.union_universes


@dataclass(eq=False, frozen=True)
class PromiseSameUniverseContext(
    Context, column_properties_evaluator=cp.PreserveDependenciesPropsEvaluator
):
    """Context of table.unsafe_promise_same_universe_as() operation."""

    orig_universe: Universe

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.orig_universe, self.universe]


@dataclass(eq=True, frozen=True)
class JoinContext(Context):
    """Context of `table.join() operation."""

    left_table: pw.Table
    right_table: pw.Table
    on_left: ContextTable
    on_right: ContextTable
    assign_id: bool
    left_ear: bool
    right_ear: bool

    def column_dependencies_internal(self) -> Iterable[Column]:
        return chain(self.on_left.columns, self.on_right.columns)

    def _get_type_interpreter(self):
        from pathway.internals.type_interpreter import JoinTypeInterpreter

        return JoinTypeInterpreter(
            self.left_table, self.right_table, self.right_ear, self.left_ear
        )

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.left_table._universe, self.right_table._universe]

    def intermediate_tables(self) -> Iterable[Table]:
        return [
            _create_internal_table(
                self.on_left.columns,
                self.on_left.universe,
                self.left_table._table_restricted_context,
            ),
            _create_internal_table(
                self.on_right.columns,
                self.on_right.universe,
                self.right_table._table_restricted_context,
            ),
        ]


@dataclass(eq=False, frozen=True)
class JoinRowwiseContext(RowwiseContext):
    temporary_column_to_original: dict[InternalColRef, InternalColRef]
    original_column_to_temporary: dict[InternalColRef, ColumnReference]

    @staticmethod
    def from_mapping(
        universe: Universe, columns_mapping: dict[InternalColRef, ColumnReference]
    ) -> JoinRowwiseContext:
        temporary_column_to_original = {}
        for orig_colref, expression in columns_mapping.items():
            temporary_column_to_original[expression._to_internal()] = orig_colref
        return JoinRowwiseContext(
            universe, temporary_column_to_original, columns_mapping.copy()
        )

    def _get_type_interpreter(self):
        from pathway.internals.type_interpreter import JoinRowwiseTypeInterpreter

        return JoinRowwiseTypeInterpreter(
            self.temporary_column_to_original, self.original_column_to_temporary
        )


@dataclass(eq=False, frozen=True)
class FlattenContext(Context):
    """Context of `table.flatten() operation."""

    orig_universe: Universe
    flatten_column: Column
    flatten_result_column: MaterializedColumn

    def column_dependencies_external(self) -> Iterable[Column]:
        return [self.flatten_column]

    @staticmethod
    def get_flatten_column_dtype(flatten_column: ColumnWithExpression):
        dtype = flatten_column.dtype
        if isinstance(dtype, dt.List):
            return dtype.wrapped
        if isinstance(dtype, dt.Tuple):
            if dtype == dt.ANY_TUPLE:
                return dt.ANY
            assert not isinstance(dtype.args, EllipsisType)
            return_dtype = dtype.args[0]
            for single_dtype in dtype.args[1:]:
                return_dtype = dt.types_lca(return_dtype, single_dtype)
            return return_dtype
        elif dtype == dt.STR:
            return dt.STR
        elif dtype in {dt.ARRAY, dt.ANY}:
            return dt.ANY
        else:
            raise TypeError(
                f"Cannot flatten column {flatten_column.expression!r} of type {dtype}."
            )

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.orig_universe]


@dataclass(eq=False, frozen=True)
class SortingContext(Context):
    """Context of table.sort() operation."""

    key_column: ColumnWithExpression
    instance_column: ColumnWithExpression
    prev_column: MaterializedColumn
    next_column: MaterializedColumn

    def column_dependencies_internal(self) -> Iterable[Column]:
        return [self.key_column, self.instance_column]

    def universe_dependencies(self) -> Iterable[Universe]:
        return [self.universe]
