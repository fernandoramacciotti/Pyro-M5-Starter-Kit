# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import math
import os
import zipfile

import numpy as np
import pandas as pd
import torch


class M5Data:
    """
    A helper class to read M5 source files and create submissions.

    :param str data_path: Path to the folder that contains M5 data files, which is
        either a single `.zip` file or some `.csv` files extracted from that zip file.
    """
    num_states = 3
    num_stores = 10
    num_cats = 3
    num_depts = 7
    num_items = 3049
    num_stores_by_state = [4, 3, 3]
    num_depts_by_cat = [2, 2, 3]
    num_items_by_cat = [565, 1047, 1437]
    num_items_by_dept = [416, 149, 532, 515, 216, 398, 823]
    num_timeseries = 30490  # store x item
    num_aggregations = 42840
    num_aggregations_by_level = [1, 3, 10, 3, 7, 9, 21, 30, 70, 3049, 9147, 30490]
    num_quantiles = 9
    quantiles = [0.005, 0.025, 0.165, 0.25, 0.5, 0.75, 0.835, 0.975, 0.995]
    aggregation_levels = [[],
                          ["state_id"],
                          ["store_id"],
                          ["cat_id"],
                          ["dept_id"],
                          ["state_id", "cat_id"],
                          ["state_id", "dept_id"],
                          ["store_id", "cat_id"],
                          ["store_id", "dept_id"],
                          ["item_id"],
                          ["state_id", "item_id"],
                          ["store_id", "item_id"]]
    event_types = ["Cultural", "National", "Religious", "Sporting"]

    def __init__(self, data_path=None):
        self.data_path = os.path.abspath("data") if data_path is None else data_path
        if not os.path.exists(self.data_path):
            raise FileNotFoundError(f"There is no folder '{self.data_path}'.")

        acc_path = os.path.join(self.data_path, "m5-forecasting-accuracy.zip")
        unc_path = os.path.join(self.data_path, "m5-forecasting-uncertainty.zip")
        self.acc_zipfile = zipfile.ZipFile(acc_path) if os.path.exists(acc_path) else None
        self.unc_zipfile = zipfile.ZipFile(unc_path) if os.path.exists(unc_path) else None

        self._sales_df = None
        self._calendar_df = None
        self._prices_df = None

    @property
    def num_days(self):
        return self.calendar_df.shape[0]

    @property
    def num_train_days(self):
        return self.sales_df.shape[1] - 5

    @property
    def sales_df(self):
        if self._sales_df is None:
            self._sales_df = self._read_csv("sales_train_validation.csv", index_col=0)
        return self._sales_df

    @property
    def calendar_df(self):
        if self._calendar_df is None:
            self._calendar_df = self._read_csv("calendar.csv", index_col=0)
        return self._calendar_df

    @property
    def prices_df(self):
        if self._prices_df is None:
            df = self._read_csv("sell_prices.csv")
            df["id"] = df.item_id + "_" + df.store_id + "_validation"
            df = pd.pivot_table(df, values="sell_price", index="id", columns="wm_yr_wk")
            self._prices_df = df.fillna(float('nan')).loc[self.sales_df.index]
        return self._prices_df

    def listdir(self):
        """
        List all files in `self.data_path` folder.
        """
        files = set(os.listdir(self.data_path))
        if self.acc_zipfile:
            files |= set(self.acc_zipfile.namelist())
        if self.unc_zipfile:
            files |= set(self.unc_zipfile.namelist())
        return files

    def _read_csv(self, filename, index_col=None, use_acc_file=True):
        """
        Returns the dataframe from csv file ``filename``.

        :param str filename: name of the file with trailing `.csv`.
        :param int index_col: indicates which column from csv file is considered as index.
        :param bool acc_file: whether to load data from accuracy.zip file or uncertainty.zip file.
        """
        assert filename.endswith(".csv")
        if filename not in self.listdir():
            raise FileNotFoundError(f"Cannot find either '{filename}' "
                                    "or 'm5-forecasting-*.zip' file "
                                    f"in '{self.data_path}'.")

        if use_acc_file and self.acc_zipfile and filename in self.acc_zipfile.namelist():
            return pd.read_csv(self.acc_zipfile.open(filename), index_col=index_col)

        if self.unc_zipfile and filename in self.unc_zipfile.namelist():
            return pd.read_csv(self.unc_zipfile.open(filename), index_col=index_col)

        return pd.read_csv(os.path.join(self.data_path, filename), index_col=index_col)

    def get_sales(self):
        """
        Returns `sales` torch.Tensor with shape `num_timeseries x num_train_days`.
        """
        return torch.from_numpy(self.sales_df.iloc[:, 5:].values).type(torch.get_default_dtype())

    def get_prices(self, fillna=0.):
        """
        Returns `prices` torch.Tensor with shape `num_timeseries x num_days`.

        In some days, there are some items not available, so their prices will be NaN.

        :param float fillna: a float value to replace NaN. Defaults to 0.
        """
        x = torch.from_numpy(self.prices_df.values).type(torch.get_default_dtype())
        x[torch.isnan(x)] = fillna
        x = x.repeat_interleave(7, dim=-1)[:, :self.calendar_df.shape[0]]
        assert x.shape == (self.num_timeseries, self.num_days)
        return x

    def get_snap(self):
        """
        Returns a `num_days x 3` boolean tensor which indicates whether
        SNAP purchases are allowed at a state in a particular day. The order
        of the first dimension indicates the states "CA", "TX", "WI" respectively.

        Usage::

            >>> m5 = M5Data()
            >>> snap = m5.get_snap()
            >>> assert snap.shape == (m5.num_states, m5.num_days)
            >>> snap = snap.repeat_interleave(torch.tensor(m5.num_stores_by_state), dim=0)
            >>> assert snap.shape == (m5.num_stores, m5.num_days)
        """
        snap = self.calendar_df[["snap_CA", "snap_TX", "snap_WI"]].values
        x = torch.from_numpy(snap).type(torch.get_default_dtype())
        assert x.shape == (self.num_days, 3)
        return x

    def get_event(self, by_types=False):
        """
        Returns a tensor with length `num_days` indicating whether there are
        special events on a particular day.

        There are 4 types of events: "Cultural", "National", "Religious", "Sporting".

        :param bool by_types: if True, returns a `num_days x 4` tensor indicating
            special event by type. Otherwise, only returns a `num_days x 1` tensor indicating
            whether there is a special event.
        """
        if not by_types:
            event = self.calendar_df["event_type_1"].notnull().values[..., None]
            x = torch.from_numpy(event).type(torch.get_default_dtype())
            assert x.shape == (self.num_days, 1)
            return x

        types = self.event_types
        event1 = pd.get_dummies(self.calendar_df["event_type_1"])[types].astype(bool)
        event2 = pd.DataFrame(columns=types)
        types2 = ["Cultural", "Religious"]
        event2[types2] = pd.get_dummies(self.calendar_df["event_type_2"])[types2].astype(bool)
        event2.fillna(False, inplace=True)
        x = torch.from_numpy(event1.values | event2.values).type(torch.get_default_dtype())
        assert x.shape == (self.num_days, 4)
        return x

    def get_dummy_day_of_month(self):
        """
        Returns dummy day of month tensor with shape `num_days x 31`.
        """
        dom = pd.get_dummies(pd.to_datetime(self.calendar_df.index).day).values
        x = torch.from_numpy(dom).type(torch.get_default_dtype())
        assert x.shape == (self.num_days, 31)
        return x

    def get_dummy_month_of_year(self):
        """
        Returns dummy month of year tensor with shape `num_days x 12`.
        """
        moy = pd.get_dummies(pd.to_datetime(self.calendar_df.index).month).values
        x = torch.from_numpy(moy).type(torch.get_default_dtype())
        assert x.shape == (self.num_days, 12)
        return x

    def get_dummy_day_of_week(self):
        """
        Returns dummy day of week tensor with shape `num_days x 7`.
        """
        dow = pd.get_dummies(self.calendar_df.wday).values
        x = torch.from_numpy(dow).type(torch.get_default_dtype())
        assert x.shape == (self.num_days, 7)
        return x

    def get_dummy_year(self):
        """
        Returns dummy year tensor with shape `num_days x 6`.
        """
        year = pd.get_dummies(pd.to_datetime(self.calendar_df.index).year).values
        x = torch.from_numpy(year).type(torch.get_default_dtype())
        assert x.shape == (self.num_days, 6)
        return x

    def get_christmas(self):
        """
        Returns a boolean 2D tensor with shape `num_days x 1` indicating
        if that day is Chrismas.
        """
        christmas = self.calendar_df.index.str.endswith("12-25")[..., None]
        x = torch.from_numpy(christmas).type(torch.get_default_dtype())
        assert x.shape == (self.num_days, 1)
        return x

    def get_dummy_state(self):
        """
        Returns dummy state tensor with shape `num_timeseries x num_states`.
        """
        state = pd.get_dummies(self.sales_df.state_id)[self.sales_df.state_id.unique()].values
        x = torch.from_numpy(state).type(torch.get_default_dtype())
        assert x.shape == (self.num_timeseries, self.num_states)
        return x

    def get_dummy_cat(self):
        """
        Returns dummy cat tensor with shape `num_timeseries x num_states`.
        """
        cat = pd.get_dummies(self.sales_df.cat_id)[self.sales_df.cat_id.unique()].values
        x = torch.from_numpy(cat).type(torch.get_default_dtype())
        assert x.shape == (self.num_timeseries, self.num_cats)
        return x

    def get_dummy_dept(self):
        """
        Returns dummy dept tensor with shape `num_timeseries x num_states`.
        """
        dept = pd.get_dummies(self.sales_df.dept_id)[self.sales_df.dept_id.unique()].values
        x = torch.from_numpy(dept).type(torch.get_default_dtype())
        assert x.shape == (self.num_timeseries, self.num_depts)
        return x

    def get_aggregated_sales(self, level):
        """
        Returns aggregated sales at a particular aggregation level.

        The result will be a tensor with shape `num_timeseries x num_train_days`.
        """
        if level == self.aggregation_levels[-1]:
            x = self.sales_df.iloc[:, 5:].values
        elif level == self.aggregation_levels[0]:
            x = self.sales_df.iloc[:, 5:].sum().values[None, :]
        else:
            df = self.sales_df.groupby(level, sort=False).sum()
            x = df.values

        return torch.from_numpy(x).type(torch.get_default_dtype())

    def get_aggregated_ma_dollar_sales(self, level):
        """
        Returns aggregated "moving average" dollar sales at a particular aggregation level
        during the last 28 days.

        The result can be used as `weight` for evaluation metrics.
        """
        prices = self.prices_df.fillna(0.).values.repeat(7, axis=1)[:, :self.sales_df.shape[1] - 5]
        df = (self.sales_df.iloc[:, 5:] * prices).T.rolling(28, min_periods=1).mean().T

        if level == self.aggregation_levels[-1]:
            x = df.values
        elif level == self.aggregation_levels[0]:
            x = df.sum().values[None, :]
        else:
            for g in level:
                df[g] = self.sales_df[g]

            df = df.groupby(level, sort=False).sum()
            x = df.values

        return torch.from_numpy(x).type(torch.get_default_dtype())

    def get_all_aggregated_sales(self):
        """
        Returns aggregated sales for all aggregation levels.
        """
        xs = []
        for level in self.aggregation_levels:
            xs.append(self.get_aggregated_sales(level))
        xs = torch.cat(xs, 0)
        assert xs.shape[0] == self.num_aggregations
        return xs

    def get_all_aggregated_ma_dollar_sales(self):
        """
        Returns aggregated "moving average" dollar sales for all aggregation levels.
        """
        xs = []
        for level in self.aggregation_levels:
            xs.append(self.get_aggregated_ma_dollar_sales(level))
        xs = torch.cat(xs, 0)
        assert xs.shape[0] == self.num_aggregations
        return xs

    def aggregate_samples(self, samples, level, *extra_levels):
        """
        Aggregates samples (at the lowest level) to a specific level.

        Usage::

            >>> m5 = M5Data()
            >>> o = []
            >>> for level in m5.aggregation_levels:
            ...     print("Level", level)
            ...     o.append(m5.aggregate_samples(samples, level))
            >>> o = torch.cat(o, 1)
            >>> q = np.quantile(o.numpy(), m5.quantiles, axis=0)  # compute quantiles
            >>> m5.make_uncertainty_submission("foo.csv", q)

        :param torch.Tensor samples: a tensor with shape `num_samples x num_timeseries x num_days`
        :param list level: which level to aggregate
        :param extra_levels: additional levels to aggregate; the results for all levels will be
            concatenated together.
        :returns: a tensor with shape `num_samples x num_aggregated_timeseries x num_days`.
        """
        assert torch.is_tensor(samples)
        assert samples.dim() == 3
        assert samples.size(1) == self.num_timeseries
        num_samples, duration = samples.size(0), samples.size(-1)
        x = samples.reshape(num_samples, self.num_stores, self.num_items, duration)

        if "state_id" in level:
            tmp = []
            pos = 0
            for n in self.num_stores_by_state:
                tmp.append(x[:, pos:pos + n].sum(1, keepdim=True))
                pos = pos + n
            x = torch.cat(tmp, dim=1)
        elif "store_id" in level:
            pass
        else:
            x = x.sum(1, keepdim=True)

        if "cat_id" in level:
            tmp = []
            pos = 0
            for n in self.num_items_by_cat:
                tmp.append(x[:, :, pos:pos + n].sum(2, keepdim=True))
                pos = pos + n
            x = torch.cat(tmp, dim=2)
        elif "dept_id" in level:
            tmp = []
            pos = 0
            for n in self.num_items_by_dept:
                tmp.append(x[:, :, pos:pos + n].sum(2, keepdim=True))
                pos = pos + n
            x = torch.cat(tmp, dim=2)
        elif "item_id" in level:
            pass
        else:
            x = x.sum(2, keepdim=True)

        n = self.num_aggregations_by_level[self.aggregation_levels.index(level)]
        x = x.reshape(num_samples, n, duration)
        if extra_levels:
            tmp = [x]
            for level in extra_levels:
                tmp.append(self.aggregate_samples(samples, level))
            x = torch.cat(tmp, 1)
        return x

    def make_accuracy_submission(self, filename, prediction):
        """
        Makes submission file given prediction result.

        :param str filename: name of the submission file.
        :param torch.Tensor predicition: the prediction tensor with shape `num_timeseries x 28`.
        """
        df = self._read_csv("sample_submission.csv", index_col=0)
        if torch.is_tensor(prediction):
            prediction = prediction.detach().cpu().numpy()
        assert isinstance(prediction, np.ndarray)
        assert prediction.shape == (self.num_timeseries, 28)
        # the later 28 days only available 1 month before the deadline
        assert df.shape[0] == prediction.shape[0] * 2
        df.iloc[:prediction.shape[0], :] = prediction
        df.to_csv(filename)

    def make_uncertainty_submission(self, filename, prediction, float_format='%.3g'):
        """
        Makes submission file given prediction result.

        :param str filename: name of the submission file.
        :param torch.Tensor predicition: the prediction tensor with shape
            `9 x num_aggregations x 28`. The first dimension indicates
            9 quantiles defined in `self.quantiles`. The second dimension
            indicates aggreated series defined in `self.aggregation_levels`,
            with corresponding order. This is also the order of
            submission file.
        """
        df = self._read_csv("sample_submission.csv", index_col=0, use_acc_file=False)
        if torch.is_tensor(prediction):
            prediction = prediction.detach().cpu().numpy()
        assert isinstance(prediction, np.ndarray)
        assert prediction.shape == (9, self.num_aggregations, 28)

        # correct the messy index in submission file
        tmp = []
        pos = 0
        for level, n in zip(self.aggregation_levels, self.num_aggregations_by_level):
            if level == self.aggregation_levels[0] or level == self.aggregation_levels[-1]:
                tmp.append(prediction[:, pos:pos+n])
            else:
                tmp_df = self.sales_df.groupby(level, sort=False)[["item_id"]].count()
                tmp_df["id"] = range(tmp_df.shape[0])
                tmp_df = tmp_df.sort_index()
                if level == self.aggregation_levels[-2]:
                    tmp_df = tmp_df.reindex(["WI", "CA", "TX"], level=0)
                new_index = tmp_df["id"].values
                tmp.append(prediction[:, pos:pos+n][:, new_index])
            pos = pos + n
        prediction = np.concatenate(tmp, axis=1)

        prediction = prediction.reshape(-1, 28)
        # the later 28 days only available 1 month before the deadline
        assert df.shape[0] == prediction.shape[0] * 2
        df.iloc[:prediction.shape[0], :] = prediction
        # use float_format to reduce the size of output file,
        # recommended at https://www.kaggle.com/c/m5-forecasting-uncertainty/discussion/135049
        df.to_csv(filename, float_format=float_format)


class BatchDataLoader:
    """
    DataLoader class which iterates over the dataset (data_x, data_y) in batch.

    Usage::

        >>> data_loader = BatchDataLoader(data_x, data_y, batch_size=1000)
        >>> for batch_x, batch_y in data_loader:
        ...     # do something with batch_x, batch_y
    """
    def __init__(self, data_x, data_y, batch_size, shuffle=True):
        super().__init__()
        self.data_x = data_x
        self.data_y = data_y
        self.batch_size = batch_size
        self.shuffle = shuffle
        assert self.data_x.size(0) == self.data_y.size(0)
        assert len(self) > 0

    @property
    def size(self):
        return self.data_x.size(0)

    def __len__(self):
        # XXX: should we remove or include the tailing data (which has len < batch_size)?
        return math.ceil(self.size / self.batch_size)

    def _sample_batch_indices(self):
        if self.shuffle:
            idx = torch.randperm(self.size)
        else:
            idx = torch.arange(self.size)
        return idx, len(self)

    def __iter__(self):
        idx, n_batches = self._sample_batch_indices()
        for i in range(n_batches):
            _slice = idx[i * self.batch_size: (i + 1) * self.batch_size]
            yield self.data_x[_slice], self.data_y[_slice]
