import time
import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Union
from typing import Tuple
from typing import Iterable
from typing import Optional
from typing import Sequence
from datetime import datetime
from datetime import timedelta

import numpy as np
from multiwrapper import multiprocessing_utils as mu
from google.cloud import bigtable
from google.api_core.retry import Retry
from google.api_core.retry import if_exception_type
from google.api_core.exceptions import Aborted
from google.api_core.exceptions import DeadlineExceeded
from google.api_core.exceptions import ServiceUnavailable
from google.cloud.bigtable.table import Table
from google.cloud.bigtable.row_set import RowSet
from google.cloud.bigtable.row_data import PartialRowData
from google.cloud.bigtable.row_filters import RowFilter
from google.cloud.bigtable.column_family import MaxVersionsGCRule

from . import utils
from ..base import ClientWithIDGen
from ... import attributes
from ... import exceptions
from ...utils import basetypes
from ...utils.serializers import serialize_uint64
from ...utils.serializers import deserialize_uint64
from ...meta import ChunkedGraphMeta
from ...meta import BigTableConfig
from ...utils.generic import get_valid_timestamp
from ....ingest import IngestConfig


class BigTableClient(bigtable.Client, ClientWithIDGen):
    def __init__(self, table_id: str, config: BigTableConfig = BigTableConfig()):
        super(BigTableClient, self).__init__(
            project=config.PROJECT, read_only=config.READ_ONLY, admin=config.ADMIN,
        )
        self._instance = self.instance(config.INSTANCE)
        self._table = self._instance.table(table_id)

    @property
    def graph_meta(self):
        return self._graph_meta

    # BASE
    def create_graph(self, meta: ChunkedGraphMeta) -> None:
        """Initialize the graph and store associated meta."""
        config = meta.graph_config
        if not config.overwrite and self._table.exists():
            ValueError(f"{self._table.table_id} already exists.")
        self._table.create()
        self._create_column_families()
        self.update_graph_meta(meta)

    def update_graph_meta(self, meta: ChunkedGraphMeta):
        self._graph_meta = meta
        row = self._mutate_row(
            attributes.GraphMeta.key, {attributes.GraphMeta.Meta: meta},
        )
        self._write([row])

    def read_graph_meta(self) -> ChunkedGraphMeta:
        row = self._read_row(attributes.GraphMeta.key)
        return row[attributes.GraphMeta.Meta][0].value

    def update_graph_provenance(self, provenance: IngestConfig):
        row = self._mutate_row(
            attributes.GraphProvenance.key,
            {attributes.GraphProvenance.Provenance: provenance},
        )
        self._write([row])

    def read_graph_provenance(self) -> IngestConfig:
        return self._read_row(attributes.GraphProvenance.key)

    def read_nodes(
        self,
        start_id=None,
        end_id=None,
        end_id_inclusive=False,
        node_ids=None,
        properties=None,
        start_time=None,
        end_time=None,
        end_time_inclusive=False,
    ):
        """
        Read nodes and their properties.
        Accepts a range of node IDs or specific node IDs.
        """
        rows = self._read_rows(
            start_key=serialize_uint64(start_id) if start_id is not None else None,
            end_key=serialize_uint64(end_id) if end_id is not None else None,
            end_key_inclusive=end_id_inclusive,
            row_keys=(serialize_uint64(node_id) for node_id in node_ids)
            if node_ids is not None
            else None,
            columns=properties,
            start_time=start_time,
            end_time=end_time,
            end_time_inclusive=end_time_inclusive,
        )
        return {deserialize_uint64(row_key): data for (row_key, data) in rows.items()}

    def read_node(
        self,
        node_id: np.uint64,
        properties: Optional[
            Union[Iterable[attributes._Attribute], attributes._Attribute]
        ] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        end_time_inclusive: bool = False,
    ) -> Union[
        Dict[attributes._Attribute, List[bigtable.row_data.Cell]],
        List[bigtable.row_data.Cell],
    ]:
        """Convenience function for reading a single node from Bigtable.
        Arguments:
            node_id {np.uint64} -- the NodeID of the row to be read.
        Keyword Arguments:
            columns {Optional[Union[Iterable[attributes._Attribute], attributes._Attribute]]} --
                Optional filtering by columns to speed up the query. If `columns` is a single
                column (not iterable), the column key will be omitted from the result.
                (default: {None})
            start_time {Optional[datetime]} -- Ignore cells with timestamp before
                `start_time`. If None, no lower bound. (default: {None})
            end_time {Optional[datetime]} -- Ignore cells with timestamp after `end_time`.
                If None, no upper bound. (default: {None})
            end_time_inclusive {bool} -- Whether or not `end_time` itself should be included in the
                request, ignored if `end_time` is None. (default: {False})
        Returns:
            Union[Dict[attributes._Attribute, List[bigtable.row_data.Cell]],
                  List[bigtable.row_data.Cell]] --
                Returns a mapping of columns to a List of cells (one cell per timestamp). Each cell
                has a `value` property, which returns the deserialized field, and a `timestamp`
                property, which returns the timestamp as `datetime` object.
                If only a single `attributes._Attribute` was requested, the List of cells is returned
                directly.
        """
        return self._read_row(
            row_key=serialize_uint64(node_id),
            columns=properties,
            start_time=start_time,
            end_time=end_time,
            end_time_inclusive=end_time_inclusive,
        )

    def write_nodes(self, nodes, root_ids=None, operation_id=None):
        """
        Writes/updates nodes (IDs along with properties)
        by locking root nodes until changes are written.
        """
        # TODO convert nodes and properties to bigtable rows
        self._write(nodes, root_ids, operation_id)

    def lock_root(self, root_id: np.uint64, operation_id: np.uint64) -> bool:
        """Attempts to lock the latest version of a root node"""
        lock_expiry = self.graph_meta.graph_config.ROOT_LOCK_EXPIRY
        lock_column = attributes.Concurrency.Lock
        combined_filter = utils.get_root_lock_filter(lock_column, lock_expiry)

        root_row = self._table.row(serialize_uint64(root_id), filter_=combined_filter)
        # Set row lock if condition returns no results (state == False)
        time_stamp = get_valid_timestamp(None)
        root_row.set_cell(
            lock_column.family_id,
            lock_column.key,
            serialize_uint64(operation_id),
            state=False,
            timestamp=time_stamp,
        )

        # The lock was acquired when set_cell returns False (state)
        lock_acquired = not root_row.commit()

        if not lock_acquired:
            row = self._read_row(root_id, columns=lock_column)
            l_operation_ids = [cell.value for cell in row]
            self.logger.debug(f"Locked operation ids: {l_operation_ids}")
        return lock_acquired

    def lock_roots(
        self,
        root_ids: Sequence[np.uint64],
        operation_id: np.uint64,
        max_tries: int = 1,
        waittime_s: float = 0.5,
    ) -> bool:
        """Attempts to lock multiple nodes with same operation id"""
        i_try = 0
        while i_try < max_tries:
            lock_acquired = False
            # Collect latest root ids
            new_root_ids: List[np.uint64] = []
            for idx in range(len(root_ids)):
                future_root_ids = self.get_future_root_ids(root_ids[idx])
                if len(future_root_ids) == 0:
                    new_root_ids.append(root_ids[idx])
                else:
                    new_root_ids.extend(future_root_ids)

            # Attempt to lock all latest root ids
            root_ids = np.unique(new_root_ids)
            for idx in range(len(root_ids)):
                self.logger.debug(
                    "operation id: %d - root id: %d" % (operation_id, root_ids[idx])
                )
                lock_acquired = self.lock_root(root_ids[idx], operation_id)
                # Roll back locks if one root cannot be locked
                if not lock_acquired:
                    for j_root_id in range(len(root_ids)):
                        self.unlock_root(root_ids[j_root_id], operation_id)
                    break

            if lock_acquired:
                return True, root_ids

            time.sleep(waittime_s)
            i_try += 1
            self.logger.debug(f"Try {i_try}")
        return False, root_ids

    def unlock_root(self, root_id: np.uint64, operation_id: np.uint64):
        """Unlocks root node that is locked with operation_id."""

        lock_column = attributes.Concurrency.Lock
        chained_filter = utils.get_unlock_root_filter(
            lock_column, self.graph_meta.graph_config.ROOT_LOCK_EXPIRY, operation_id
        )
        # Get conditional row using the chained filter
        root_row = self._table.row(serialize_uint64(root_id), filter_=chained_filter)

        # Delete row if conditions are met (state == True)
        root_row.delete_cell(lock_column.family_id, lock_column.key, state=True)
        return root_row.commit()

    def renew_lock(self, root_id: np.uint64, operation_id: np.uint64) -> bool:
        """Renews existing root node lock with operation_id to extend time."""
        lock_column = attributes.Concurrency.Lock
        root_row = self._table.row(
            serialize_uint64(root_id),
            filter_=utils.get_renew_lock_filter(lock_column, operation_id),
        )
        # Set row lock if condition returns a result (state == True)
        root_row.set_cell(
            lock_column.family_id,
            lock_column.key,
            lock_column.serialize(operation_id),
            state=False,
        )

        # The lock was acquired when set_cell returns True (state)
        return not root_row.commit()

    def renew_locks(self, root_ids: np.uint64, operation_id: np.uint64) -> bool:
        """Renews existing root node locks with operation_id to extend time."""
        for root_id in root_ids:
            if not self.renew_lock(root_id, operation_id):
                self.logger.warning(f"renew_lock failed - {root_id}")
                return False
        return True

    # IDs
    def create_node_ids(self, chunk_id: np.uint64, size: int) -> np.ndarray:
        """Generates a list of unique node IDs for the given chunk."""
        low, high = self._get_ids_range(serialize_uint64(chunk_id), size)
        low_id, high_id = chunk_id | low, chunk_id | high
        return np.arange(low_id, high_id + np.uint64(1), dtype=basetypes.NODE_ID)

    def create_node_id(self, chunk_id: np.uint64) -> basetypes.NODE_ID:
        """Generate a unique node ID in the chunk."""
        return self.create_node_ids(chunk_id, 1)[0]

    def get_max_node_id(self, chunk_id: basetypes.CHUNK_ID) -> basetypes.NODE_ID:
        """Gets the current maximum segment ID in the chunk."""
        column = attributes.Concurrency.Counter
        row = self._read_row(serialize_uint64(chunk_id), columns=column)
        return chunk_id | basetypes.SEGMENT_ID.type(row[0].value if row else 0)

    def create_operation_id(self):
        """Generate a unique operation ID."""
        return self._get_ids_range(attributes.OperationLogs.key, 1)[1]

    def get_max_operation_id(self):
        """Gets the current maximum operation ID."""
        column = attributes.Concurrency.Counter
        row = self._read_row(attributes.OperationLogs.key, columns=column)
        return row[0].value if row else column.basetype(0)

    # PRIVATE METHODS
    def _create_column_families(self):
        # TODO hardcoded, not good
        f = self._table.column_family("0")
        f.create()
        f = self._table.column_family("1", gc_rule=MaxVersionsGCRule(1))
        f.create()
        f = self._table.column_family("2")
        f.create()
        f = self._table.column_family("3", gc_rule=MaxVersionsGCRule(1))
        f.create()

    def _get_ids_range(self, key: bytes, size: int):
        """Returns a range (min, max) of IDs for a given `key`."""
        column = attributes.Concurrency.Counter
        row = self._table.row(key, append=True)
        row.increment_cell_value(column.family_id, column.key, size)

        row = row.commit()
        high = column.deserialize(row[column.family_id][column.key][0][0])
        return high + np.uint64(1) - size, high

    def _read_rows(
        self,
        start_key: Optional[bytes] = None,
        end_key: Optional[bytes] = None,
        end_key_inclusive: bool = False,
        row_keys: Optional[Iterable[bytes]] = None,
        columns: Optional[
            Union[Iterable[attributes._Attribute], attributes._Attribute]
        ] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        end_time_inclusive: bool = False,
    ) -> Dict[
        bytes,
        Union[
            Dict[attributes._Attribute, List[bigtable.row_data.Cell]],
            List[bigtable.row_data.Cell],
        ],
    ]:
        """Main function for reading a row range or non-contiguous row sets from Bigtable using
        `bytes` keys.

        Keyword Arguments:
            start_key {Optional[bytes]} -- The first row to be read, ignored if `row_keys` is set.
                If None, no lower boundary is used. (default: {None})
            end_key {Optional[bytes]} -- The end of the row range, ignored if `row_keys` is set.
                If None, no upper boundary is used. (default: {None})
            end_key_inclusive {bool} -- Whether or not `end_key` itself should be included in the
                request, ignored if `row_keys` is set or `end_key` is None. (default: {False})
            row_keys {Optional[Iterable[bytes]]} -- An `Iterable` containing possibly
                non-contiguous row keys. Takes precedence over `start_key` and `end_key`.
                (default: {None})
            columns {Optional[Union[Iterable[attributes._Attribute], attributes._Attribute]]} --
                Optional filtering by columns to speed up the query. If `columns` is a single
                column (not iterable), the column key will be omitted from the result.
                (default: {None})
            start_time {Optional[datetime]} -- Ignore cells with timestamp before
                `start_time`. If None, no lower bound. (default: {None})
            end_time {Optional[datetime]} -- Ignore cells with timestamp after `end_time`.
                If None, no upper bound. (default: {None})
            end_time_inclusive {bool} -- Whether or not `end_time` itself should be included in the
                request, ignored if `end_time` is None. (default: {False})

        Returns:
            Dict[bytes, Union[Dict[attributes._Attribute, List[bigtable.row_data.Cell]],
                              List[bigtable.row_data.Cell]]] --
                Returns a dictionary of `byte` rows as keys. Their value will be a mapping of
                columns to a List of cells (one cell per timestamp). Each cell has a `value`
                property, which returns the deserialized field, and a `timestamp` property, which
                returns the timestamp as `datetime` object.
                If only a single `attributes._Attribute` was requested, the List of cells will be
                attached to the row dictionary directly (skipping the column dictionary).
        """

        # Create filters: Rows
        row_set = RowSet()
        if row_keys is not None:
            for row_key in row_keys:
                row_set.add_row_key(row_key)
        elif start_key is not None and end_key is not None:
            row_set.add_row_range_from_keys(
                start_key=start_key,
                start_inclusive=True,
                end_key=end_key,
                end_inclusive=end_key_inclusive,
            )
        else:
            raise exceptions.PreconditionError(
                "Need to either provide a valid set of rows, or"
                " both, a start row and an end row."
            )

        filter_ = utils.get_time_range_and_column_filter(
            columns=columns,
            start_time=start_time,
            end_time=end_time,
            end_inclusive=end_time_inclusive,
        )
        # Bigtable read with retries
        rows = self._read(row_set=row_set, row_filter=filter_)

        # Deserialize cells
        for row_key, column_dict in rows.items():
            for column, cell_entries in column_dict.items():
                for cell_entry in cell_entries:
                    cell_entry.value = column.deserialize(cell_entry.value)
            # If no column array was requested, reattach single column's values directly to the row
            if isinstance(columns, attributes._Attribute):
                rows[row_key] = cell_entries
        return rows

    def _read_row(
        self,
        row_key: bytes,
        columns: Optional[
            Union[Iterable[attributes._Attribute], attributes._Attribute]
        ] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        end_time_inclusive: bool = False,
    ) -> Union[
        Dict[attributes._Attribute, List[bigtable.row_data.Cell]],
        List[bigtable.row_data.Cell],
    ]:
        """Convenience function for reading a single row from Bigtable using its `bytes` keys.

        Arguments:
            row_key {bytes} -- The row to be read.

        Keyword Arguments:
            columns {Optional[Union[Iterable[attributes._Attribute], attributes._Attribute]]} --
                Optional filtering by columns to speed up the query. If `columns` is a single
                column (not iterable), the column key will be omitted from the result.
                (default: {None})
            start_time {Optional[datetime]} -- Ignore cells with timestamp before
                `start_time`. If None, no lower bound. (default: {None})
            end_time {Optional[datetime]} -- Ignore cells with timestamp after `end_time`.
                If None, no upper bound. (default: {None})
            end_time_inclusive {bool} -- Whether or not `end_time` itself should be included in the
                request, ignored if `end_time` is None. (default: {False})

        Returns:
            Union[Dict[attributes._Attribute, List[bigtable.row_data.Cell]],
                  List[bigtable.row_data.Cell]] --
                Returns a mapping of columns to a List of cells (one cell per timestamp). Each cell
                has a `value` property, which returns the deserialized field, and a `timestamp`
                property, which returns the timestamp as `datetime` object.
                If only a single `attributes._Attribute` was requested, the List of cells is returned
                directly.
        """
        row = self._read_rows(
            row_keys=[row_key],
            columns=columns,
            start_time=start_time,
            end_time=end_time,
            end_time_inclusive=end_time_inclusive,
        )
        return (
            row.get(row_key, [])
            if isinstance(columns, attributes._Attribute)
            else row.get(row_key, {})
        )

    def _execute_read_thread(self, args: Tuple[Table, RowSet, RowFilter]):
        table, row_set, row_filter = args
        if not row_set.row_keys and not row_set.row_ranges:
            # Check for everything falsy, because Bigtable considers even empty
            # lists of row_keys as no upper/lower bound!
            return {}

        range_read = table.read_rows(row_set=row_set, filter_=row_filter)
        res = {v.row_key: utils.partial_row_data_to_column_dict(v) for v in range_read}
        return res

    def _read(
        self, row_set: RowSet, row_filter: RowFilter = None
    ) -> Dict[bytes, Dict[attributes._Attribute, bigtable.row_data.PartialRowData]]:
        """ Core function to read rows from Bigtable. Uses standard Bigtable retry logic
        :param row_set: BigTable RowSet
        :param row_filter: BigTable RowFilter
        :return: Dict[bytes, Dict[attributes._Attribute, bigtable.row_data.PartialRowData]]
        """
        # FIXME: Bigtable limits the length of the serialized request to 512 KiB. We should
        # calculate this properly (range_read.request.SerializeToString()), but this estimate is
        # good enough for now
        # TODO try async/await
        max_row_key_count = 20000
        n_subrequests = max(1, int(np.ceil(len(row_set.row_keys) / max_row_key_count)))
        n_threads = min(n_subrequests, 2 * mu.n_cpus)

        row_sets = []
        for i in range(n_subrequests):
            r = RowSet()
            r.row_keys = row_set.row_keys[
                i * max_row_key_count : (i + 1) * max_row_key_count
            ]
            row_sets.append(r)

        # Don't forget the original RowSet's row_ranges
        row_sets[0].row_ranges = row_set.row_ranges
        responses = mu.multithread_func(
            self._execute_read_thread,
            params=((self._table, r, row_filter) for r in row_sets),
            n_threads=n_threads,
        )

        combined_response = {}
        for resp in responses:
            combined_response.update(resp)
        return combined_response

    def _write(
        self,
        rows: Iterable[bigtable.row.DirectRow],
        root_ids: Optional[Union[np.uint64, Iterable[np.uint64]]] = None,
        operation_id: Optional[np.uint64] = None,
        slow_retry: bool = True,
        block_size: int = 2000,
    ):
        """ Writes a list of mutated rows in bulk
        WARNING: If <rows> contains the same row (same row_key) and column
        key two times only the last one is effectively written to the BigTable
        (even when the mutations were applied to different columns)
        --> no versioning!
        :param rows: list
            list of mutated rows
        :param root_ids: list if uint64
        :param operation_id: uint64 or None
            operation_id (or other unique id) that *was* used to lock the root
            the bulk write is only executed if the root is still locked with
            the same id.
        :param slow_retry: bool
        :param block_size: int
        """
        if slow_retry:
            initial = 5
        else:
            initial = 1

        exception_types = (Aborted, DeadlineExceeded, ServiceUnavailable)
        retry = Retry(
            predicate=if_exception_type(exception_types),
            initial=initial,
            maximum=15.0,
            multiplier=2.0,
            deadline=self.graph_meta.graph_config.ROOT_LOCK_EXPIRY.seconds,
        )

        if root_ids is not None and operation_id is not None:
            if isinstance(root_ids, int):
                root_ids = [root_ids]
            if not self.renew_locks(root_ids, operation_id):
                raise exceptions.LockingError(
                    f"Root lock renewal failed: operation {operation_id}"
                )

        for i in range(0, len(rows), block_size):
            status = self._table.mutate_rows(rows[i : i + block_size], retry=retry)
            if not all(status):
                raise exceptions.ChunkedGraphError(
                    f"Bulk write failed: operation {operation_id}"
                )

    def _mutate_row(
        self,
        row_key: bytes,
        val_dict: Dict[attributes._Attribute, Any],
        time_stamp: Optional[datetime] = None,
    ) -> bigtable.row.Row:
        """ Mutates a single row (doesn't actually write to big table)
        :param row_key: serialized bigtable row key
        :param val_dict: Dict[attributes._Attribute: Any]
        :param time_stamp: None or datetime
        :return: list
        """
        row = self._table.row(row_key)
        for column, value in val_dict.items():
            row.set_cell(
                column_family_id=column.family_id,
                column=column.key,
                value=column.serialize(value),
                timestamp=time_stamp,
            )
        return row
