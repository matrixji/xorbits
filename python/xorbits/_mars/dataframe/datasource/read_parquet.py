# Copyright 2022-2023 XProbe Inc.
# derived from copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from typing import Dict
from urllib.parse import urlparse

import numpy as np
import pandas as pd

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = None

try:
    import fastparquet
except ImportError:
    fastparquet = None

from ... import opcodes as OperandDef
from ...config import options
from ...lib.filesystem import FileSystem, file_size, get_fs, glob, open_file
from ...serialization.serializables import (
    AnyField,
    BoolField,
    DictField,
    Int32Field,
    Int64Field,
    ListField,
    StringField,
)
from ...utils import is_object_dtype, lazy_import
from ..arrays import ArrowStringDtype
from ..operands import OutputType
from ..utils import contain_arrow_dtype, parse_index, to_arrow_dtypes
from .core import (
    ColumnPruneSupportedDataSourceMixin,
    IncrementalIndexDatasource,
    IncrementalIndexDataSourceMixin,
    merge_small_files,
)

PARQUET_MEMORY_SCALE = 15
STRING_FIELD_OVERHEAD = 50
cudf = lazy_import("cudf")


def check_engine(engine):
    if engine == "auto":
        if pa is not None:
            return "pyarrow"
        elif fastparquet is not None:  # pragma: no cover
            return "fastparquet"
        else:  # pragma: no cover
            raise RuntimeError("Please install either pyarrow or fastparquet.")
    elif engine == "pyarrow":
        if pa is None:  # pragma: no cover
            raise RuntimeError("Please install pyarrow first.")
        return engine
    elif engine == "fastparquet":
        if fastparquet is None:  # pragma: no cover
            raise RuntimeError("Please install fastparquet first.")
        return engine
    else:  # pragma: no cover
        raise RuntimeError("Unsupported engine {} to read parquet.".format(engine))


def get_engine(engine):
    if engine == "pyarrow":
        return ArrowEngine()
    elif engine == "fastparquet":
        return FastpaquetEngine()
    else:  # pragma: no cover
        raise RuntimeError("Unsupported engine {}".format(engine))


class ParquetEngine:
    def get_row_num(self, f):
        raise NotImplementedError

    def read_dtypes(self, f, **kwargs):
        raise NotImplementedError

    def read_to_pandas(
        self, f, columns=None, nrows=None, use_arrow_dtype=None, **kwargs
    ):
        raise NotImplementedError

    def read_group_to_pandas(
        self, f, group_index, columns=None, nrows=None, use_arrow_dtype=None, **kwargs
    ):
        raise NotImplementedError

    def read_partitioned_to_pandas(
        self,
        f,
        partitions: Dict,
        partition_keys: Dict,
        columns=None,
        nrows=None,
        use_arrow_dtype=None,
        **kwargs,
    ):
        raw_df = self.read_to_pandas(
            f, columns=columns, nrows=nrows, use_arrow_dtype=use_arrow_dtype, **kwargs
        )
        for col, value in partition_keys.items():
            dictionary = partitions[col]
            raw_df[col] = pd.Series(
                value,
                dtype=pd.CategoricalDtype(categories=dictionary.tolist()),
                index=raw_df.index,
            )
        return raw_df

    def read_partitioned_dtypes(self, fs: FileSystem, directory, storage_options):
        # As ParquetDataset will iterate all files,
        # here we just find one file to infer dtypes
        current_path = directory
        partition_cols = []
        while fs.isdir(current_path):
            _, dirs, files = next(fs.walk(current_path))
            dirs = [d for d in dirs if not d.startswith(".")]
            files = [f for f in files if not f.startswith(".")]
            if len(files) == 0:
                # directory as partition
                partition_cols.append(dirs[0].split("=", 1)[0])
                current_path = os.path.join(current_path, dirs[0])
            elif len(dirs) == 0:
                # parquet files in deepest directory
                current_path = os.path.join(current_path, files[0])
            else:  # pragma: no cover
                raise ValueError(
                    "Files and directories are mixed in an intermediate directory"
                )

        # current path is now a parquet file
        with open_file(current_path, storage_options=storage_options) as f:
            dtypes = self.read_dtypes(f)
        for partition in partition_cols:
            dtypes[partition] = pd.CategoricalDtype()
        return dtypes


def _parse_prefix(path):
    path_prefix = ""
    if isinstance(path, str):
        parsed_path = urlparse(path)
        if parsed_path.scheme:
            path_prefix = f"{parsed_path.scheme}://{parsed_path.netloc}"
    return path_prefix


class ArrowEngine(ParquetEngine):
    def get_row_num(self, f):
        file = pq.ParquetFile(f)
        return file.metadata.num_rows

    def read_dtypes(self, f, **kwargs):
        file = pq.ParquetFile(f)
        return file.schema_arrow.empty_table().to_pandas().dtypes

    @classmethod
    def _table_to_pandas(cls, t, nrows=None, use_arrow_dtype=None):
        if nrows is not None:
            t = t.slice(0, nrows)
        if use_arrow_dtype:
            df = t.to_pandas(types_mapper={pa.string(): ArrowStringDtype()}.get)
        else:
            df = t.to_pandas()
        return df

    def read_to_pandas(
        self, f, columns=None, nrows=None, use_arrow_dtype=None, **kwargs
    ):
        file = pq.ParquetFile(f)
        t = file.read(columns=columns, **kwargs)
        return self._table_to_pandas(t, nrows=nrows, use_arrow_dtype=use_arrow_dtype)

    def read_group_to_pandas(
        self, f, group_index, columns=None, nrows=None, use_arrow_dtype=None, **kwargs
    ):
        file = pq.ParquetFile(f)
        t = file.read_row_group(group_index, columns=columns, **kwargs)
        return self._table_to_pandas(t, nrows=nrows, use_arrow_dtype=use_arrow_dtype)


class FastpaquetEngine(ParquetEngine):
    def get_row_num(self, f):
        file = fastparquet.ParquetFile(f)
        return file.count()

    def read_dtypes(self, f, **kwargs):
        file = fastparquet.ParquetFile(f)
        dtypes_dict = file._dtypes()
        return pd.Series(dict((c, dtypes_dict[c]) for c in file.columns))

    def read_to_pandas(
        self, f, columns=None, nrows=None, use_arrow_dtype=None, **kwargs
    ):
        file = fastparquet.ParquetFile(f)
        df = file.to_pandas(columns, **kwargs)
        if nrows is not None:
            df = df.head(nrows)
        if use_arrow_dtype:
            df = df.astype(to_arrow_dtypes(df.dtypes).to_dict())
        return df


class CudfEngine:
    @classmethod
    def read_to_cudf(cls, file, columns: list = None, nrows: int = None, **kwargs):
        df = cudf.read_parquet(file, columns=columns, **kwargs)
        if nrows is not None:
            df = df.head(nrows)
        return df

    def read_group_to_cudf(
        self, file, group_index: int, columns: list = None, nrows: int = None, **kwargs
    ):
        return self.read_to_cudf(
            file, columns=columns, nrows=nrows, row_groups=group_index, **kwargs
        )

    @classmethod
    def read_partitioned_to_cudf(
        cls,
        file,
        partitions: Dict,
        partition_keys: Dict,
        columns=None,
        nrows=None,
        **kwargs,
    ):
        # cudf will read entire partitions even if only one partition provided,
        # so we just read with pyarrow and convert to cudf DataFrame
        file = pq.ParquetFile(file)
        t = file.read(columns=columns, **kwargs)
        t = t.slice(0, nrows) if nrows is not None else t
        t = pa.table(t.columns, names=t.column_names)
        raw_df = cudf.DataFrame.from_arrow(t)
        for col, value in partition_keys.items():
            dictionary = partitions[col].tolist()
            codes = cudf.core.column.as_column(
                dictionary.index(value), length=len(raw_df)
            )
            raw_df[col] = cudf.core.column.build_categorical_column(
                categories=dictionary,
                codes=codes,
                size=codes.size,
                offset=codes.offset,
                ordered=False,
            )
        return raw_df


class DataFrameReadParquet(
    IncrementalIndexDatasource,
    ColumnPruneSupportedDataSourceMixin,
    IncrementalIndexDataSourceMixin,
):
    _op_type_ = OperandDef.READ_PARQUET

    path = AnyField("path")
    engine = StringField("engine")
    columns = ListField("columns")
    use_arrow_dtype = BoolField("use_arrow_dtype")
    groups_as_chunks = BoolField("groups_as_chunks")
    group_index = Int32Field("group_index")
    read_kwargs = DictField("read_kwargs")
    incremental_index = BoolField("incremental_index")
    storage_options = DictField("storage_options")
    is_partitioned = BoolField("is_partitioned")
    merge_small_files = BoolField("merge_small_files")
    merge_small_file_options = DictField("merge_small_file_options")
    # for chunk
    partitions = DictField("partitions", default=None)
    partition_keys = DictField("partition_keys", default=None)
    num_group_rows = Int64Field("num_group_rows", default=None)
    # as read meta may be too time-consuming when number of files is large,
    # thus we only read first file to get row number and raw file size
    first_chunk_row_num = Int64Field("first_chunk_row_num")
    first_chunk_raw_bytes = Int64Field("first_chunk_raw_bytes")

    def get_columns(self):
        return self.columns

    def set_pruned_columns(self, columns, *, keep_order=None):
        self.columns = columns

    @classmethod
    def _to_arrow_dtypes(cls, dtypes, op):
        if (
            op.use_arrow_dtype is None
            and not op.gpu
            and options.dataframe.use_arrow_dtype
        ):  # pragma: no cover
            # check if use_arrow_dtype set on the server side
            dtypes = to_arrow_dtypes(dtypes)
        return dtypes

    @classmethod
    def _tile_partitioned(cls, op: "DataFrameReadParquet"):
        out_df = op.outputs[0]
        shape = (np.nan, out_df.shape[1])
        dtypes = cls._to_arrow_dtypes(out_df.dtypes, op)
        dataset = pq.ParquetDataset(op.path, use_legacy_dataset=False)

        path_prefix = _parse_prefix(op.path)

        chunk_index = 0
        out_chunks = []
        first_chunk_row_num, first_chunk_raw_bytes = None, None
        for i, fragment in enumerate(dataset.fragments):
            chunk_op = op.copy().reset_key()
            chunk_op.path = chunk_path = path_prefix + fragment.path
            relpath = os.path.relpath(chunk_path, op.path)
            partition_keys = dict(
                tuple(s.split("=")) for s in relpath.split(os.sep)[:-1]
            )
            chunk_op.partition_keys = partition_keys
            chunk_op.partitions = dict(
                zip(
                    dataset.partitioning.schema.names, dataset.partitioning.dictionaries
                )
            )
            if i == 0:
                first_row_group = fragment.row_groups[0]
                first_chunk_raw_bytes = first_row_group.total_byte_size
                first_chunk_row_num = first_row_group.num_rows
            chunk_op.first_chunk_row_num = first_chunk_row_num
            chunk_op.first_chunk_raw_bytes = first_chunk_raw_bytes
            new_chunk = chunk_op.new_chunk(
                None,
                shape=shape,
                index=(chunk_index, 0),
                index_value=out_df.index_value,
                columns_value=out_df.columns_value,
                dtypes=dtypes,
            )
            out_chunks.append(new_chunk)
            chunk_index += 1

        new_op = op.copy()
        nsplits = ((np.nan,) * len(out_chunks), (out_df.shape[1],))
        return new_op.new_dataframes(
            None,
            out_df.shape,
            dtypes=dtypes,
            index_value=out_df.index_value,
            columns_value=out_df.columns_value,
            chunks=out_chunks,
            nsplits=nsplits,
        )

    @classmethod
    def _tile_no_partitioned(cls, op: "DataFrameReadParquet"):
        chunk_index = 0
        out_chunks = []
        out_df = op.outputs[0]

        dtypes = cls._to_arrow_dtypes(out_df.dtypes, op)
        shape = (np.nan, out_df.shape[1])

        path_prefix = ""
        if isinstance(op.path, (tuple, list)):
            paths = op.path
        elif get_fs(op.path, op.storage_options).isdir(op.path):
            parsed_path = urlparse(op.path)
            if parsed_path.scheme.lower() == "hdfs":
                path_prefix = f"{parsed_path.scheme}://{parsed_path.netloc}"
            paths = get_fs(op.path, op.storage_options).ls(op.path)
        else:
            paths = glob(op.path, storage_options=op.storage_options)

        first_chunk_row_num, first_chunk_raw_bytes = None, None
        for i, pth in enumerate(paths):
            pth = path_prefix + pth
            if i == 0:
                with open_file(pth, storage_options=op.storage_options) as f:
                    first_chunk_row_num = get_engine(op.engine).get_row_num(f)
                first_chunk_raw_bytes = file_size(
                    pth, storage_options=op.storage_options
                )

            if op.groups_as_chunks:
                num_row_groups = pq.ParquetFile(pth).num_row_groups
                for group_idx in range(num_row_groups):
                    chunk_op = op.copy().reset_key()
                    chunk_op.path = pth
                    chunk_op.group_index = group_idx
                    chunk_op.first_chunk_row_num = first_chunk_row_num
                    chunk_op.first_chunk_raw_bytes = first_chunk_raw_bytes
                    chunk_op.num_group_rows = num_row_groups
                    new_chunk = chunk_op.new_chunk(
                        None,
                        shape=shape,
                        index=(chunk_index, 0),
                        index_value=out_df.index_value,
                        columns_value=out_df.columns_value,
                        dtypes=dtypes,
                    )
                    out_chunks.append(new_chunk)
                    chunk_index += 1
            else:
                chunk_op = op.copy().reset_key()
                chunk_op.path = pth
                chunk_op.first_chunk_row_num = first_chunk_row_num
                chunk_op.first_chunk_raw_bytes = first_chunk_raw_bytes
                new_chunk = chunk_op.new_chunk(
                    None,
                    shape=shape,
                    index=(chunk_index, 0),
                    index_value=out_df.index_value,
                    columns_value=out_df.columns_value,
                    dtypes=dtypes,
                )
                out_chunks.append(new_chunk)
                chunk_index += 1

        new_op = op.copy()
        nsplits = ((np.nan,) * len(out_chunks), (out_df.shape[1],))
        return new_op.new_dataframes(
            None,
            out_df.shape,
            dtypes=dtypes,
            index_value=out_df.index_value,
            columns_value=out_df.columns_value,
            chunks=out_chunks,
            nsplits=nsplits,
        )

    @classmethod
    def _tile(cls, op: "DataFrameReadParquet"):
        if op.is_partitioned:
            tiled = cls._tile_partitioned(op)
        else:
            tiled = cls._tile_no_partitioned(op)
        if op.merge_small_files:
            tiled = [
                merge_small_files(tiled[0], **(op.merge_small_file_options or dict()))
            ]
        return tiled

    @classmethod
    def _execute_partitioned(cls, ctx, op: "DataFrameReadParquet"):
        out = op.outputs[0]
        engine = get_engine(op.engine)
        with open_file(op.path, storage_options=op.storage_options) as f:
            ctx[out.key] = engine.read_partitioned_to_pandas(
                f,
                op.partitions,
                op.partition_keys,
                columns=op.columns,
                nrows=op.nrows,
                use_arrow_dtype=op.use_arrow_dtype,
                **op.read_kwargs or dict(),
            )

    @classmethod
    def _pandas_read_parquet(cls, ctx: dict, op: "DataFrameReadParquet"):
        out = op.outputs[0]
        path = op.path

        if op.partitions is not None:
            return cls._execute_partitioned(ctx, op)

        engine = get_engine(op.engine)
        with open_file(path, storage_options=op.storage_options) as f:
            use_arrow_dtype = contain_arrow_dtype(out.dtypes)
            if op.groups_as_chunks:
                df = engine.read_group_to_pandas(
                    f,
                    op.group_index,
                    columns=op.columns,
                    nrows=op.nrows,
                    use_arrow_dtype=use_arrow_dtype,
                    **op.read_kwargs or dict(),
                )
            else:
                df = engine.read_to_pandas(
                    f,
                    columns=op.columns,
                    nrows=op.nrows,
                    use_arrow_dtype=use_arrow_dtype,
                    **op.read_kwargs or dict(),
                )

            ctx[out.key] = df

    @classmethod
    def _cudf_read_parquet(cls, ctx: dict, op: "DataFrameReadParquet"):
        out = op.outputs[0]
        path = op.path

        engine = CudfEngine()
        if os.path.exists(path):
            file = op.path
            close = lambda: None
        else:  # pragma: no cover
            file = open_file(path, storage_options=op.storage_options)
            close = file.close

        try:
            if op.partitions is not None:
                ctx[out.key] = engine.read_partitioned_to_cudf(
                    file,
                    op.partitions,
                    op.partition_keys,
                    columns=op.columns,
                    nrows=op.nrows,
                    **op.read_kwargs or dict(),
                )
            else:
                if op.groups_as_chunks:
                    df = engine.read_group_to_cudf(
                        file,
                        op.group_index,
                        columns=op.columns,
                        nrows=op.nrows,
                        **op.read_kwargs or dict(),
                    )
                else:
                    df = engine.read_to_cudf(
                        file,
                        columns=op.columns,
                        nrows=op.nrows,
                        **op.read_kwargs or dict(),
                    )
                ctx[out.key] = df
        finally:
            close()

    @classmethod
    def execute(cls, ctx, op: "DataFrameReadParquet"):
        if not op.gpu:
            cls._pandas_read_parquet(ctx, op)
        else:
            cls._cudf_read_parquet(ctx, op)

    @classmethod
    def estimate_size(cls, ctx, op: "DataFrameReadParquet"):
        first_chunk_row_num = op.first_chunk_row_num
        first_chunk_raw_bytes = op.first_chunk_raw_bytes
        raw_bytes = file_size(op.path, storage_options=op.storage_options)
        if op.num_group_rows:
            raw_bytes = (
                np.ceil(np.divide(raw_bytes, op.num_group_rows)).astype(np.int64).item()
            )

        estimated_row_num = (
            np.ceil(first_chunk_row_num * (raw_bytes / first_chunk_raw_bytes))
            .astype(np.int64)
            .item()
        )
        phy_size = raw_bytes * (op.memory_scale or PARQUET_MEMORY_SCALE)
        n_strings = len([dt for dt in op.outputs[0].dtypes if is_object_dtype(dt)])
        pd_size = phy_size + n_strings * estimated_row_num * STRING_FIELD_OVERHEAD
        ctx[op.outputs[0].key] = (pd_size, pd_size + phy_size)

    def __call__(self, index_value=None, columns_value=None, dtypes=None):
        self._output_types = [OutputType.dataframe]
        shape = (np.nan, len(dtypes))
        return self.new_dataframe(
            None,
            shape,
            dtypes=dtypes,
            index_value=index_value,
            columns_value=columns_value,
        )


def read_parquet(
    path,
    engine: str = "auto",
    columns: list = None,
    groups_as_chunks: bool = False,
    use_arrow_dtype: bool = None,
    incremental_index: bool = False,
    storage_options: dict = None,
    memory_scale: int = None,
    merge_small_files: bool = True,
    merge_small_file_options: dict = None,
    gpu: bool = None,
    **kwargs,
):
    """
    Load a parquet object from the file path, returning a DataFrame.

    Parameters
    ----------
    path : str, path object or file-like object
        Any valid string path is acceptable. The string could be a URL.
        For file URLs, a host is expected. A local file could be:
        ``file://localhost/path/to/table.parquet``.
        A file URL can also be a path to a directory that contains multiple
        partitioned parquet files. Both pyarrow and fastparquet support
        paths to directories as well as file URLs. A directory path could be:
        ``file://localhost/path/to/tables``.
        By file-like object, we refer to objects with a ``read()`` method,
        such as a file handler (e.g. via builtin ``open`` function)
        or ``StringIO``.
    engine : {'auto', 'pyarrow', 'fastparquet'}, default 'auto'
        Parquet library to use. The default behavior is to try 'pyarrow',
        falling back to 'fastparquet' if 'pyarrow' is unavailable.
    columns : list, default=None
        If not None, only these columns will be read from the file.
    groups_as_chunks : bool, default False
        if True, each row group correspond to a chunk.
        if False, each file correspond to a chunk.
        Only available for 'pyarrow' engine.
    incremental_index: bool, default False
        If index_col not specified, ensure range index incremental,
        gain a slightly better performance if setting False.
    use_arrow_dtype: bool, default None
        If True, use arrow dtype to store columns.
    storage_options: dict, optional
        Options for storage connection.
    memory_scale: int, optional
        Scale that real memory occupation divided with raw file size.
    merge_small_files: bool, default True
        Merge small files whose size is small.
    merge_small_file_options: dict
        Options for merging small files
    **kwargs
        Any additional kwargs are passed to the engine.

    Returns
    -------
    Mars DataFrame
    """

    engine_type = check_engine(engine)
    engine = get_engine(engine_type)

    single_path = path[0] if isinstance(path, list) else path
    fs = get_fs(single_path, storage_options)
    is_partitioned = False
    if fs.isdir(single_path):
        paths = fs.ls(path)
        if all(fs.isdir(p) for p in paths):
            # If all are directories, it is read as a partitioned dataset.
            dtypes = engine.read_partitioned_dtypes(fs, path, storage_options)
            is_partitioned = True
        else:
            with fs.open(paths[0], mode="rb") as f:
                dtypes = engine.read_dtypes(f)
    else:
        if not isinstance(path, list):
            file_path = glob(path, storage_options=storage_options)[0]
        else:
            file_path = path[0]

        with open_file(file_path, storage_options=storage_options) as f:
            dtypes = engine.read_dtypes(f)

    if columns:
        dtypes = dtypes[columns]

    if use_arrow_dtype is None:
        use_arrow_dtype = options.dataframe.use_arrow_dtype
    if use_arrow_dtype:
        dtypes = to_arrow_dtypes(dtypes)

    index_value = parse_index(pd.RangeIndex(-1))
    columns_value = parse_index(dtypes.index, store_data=True)
    op = DataFrameReadParquet(
        path=path,
        engine=engine_type,
        columns=columns,
        groups_as_chunks=groups_as_chunks,
        use_arrow_dtype=use_arrow_dtype,
        read_kwargs=kwargs,
        incremental_index=incremental_index,
        storage_options=storage_options,
        is_partitioned=is_partitioned,
        memory_scale=memory_scale,
        merge_small_files=merge_small_files,
        merge_small_file_options=merge_small_file_options,
        gpu=gpu,
    )
    return op(index_value=index_value, columns_value=columns_value, dtypes=dtypes)
