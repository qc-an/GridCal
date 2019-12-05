# This file is part of GridCal.
#
# GridCal is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GridCal is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GridCal.  If not, see <http://www.gnu.org/licenses/>.

import json
import pandas as pd
import numpy as np
import time
import multiprocessing

from PySide2.QtCore import QThread, QThreadPool, Signal

from GridCal.Engine.basic_structures import Logger
from GridCal.Engine.Simulations.PowerFlow.power_flow_results import PowerFlowResults
from GridCal.Engine.Simulations.result_types import ResultTypes
from GridCal.Engine.Core.multi_circuit import MultiCircuit
from GridCal.Engine.Simulations.PowerFlow.power_flow_options import PowerFlowOptions
from GridCal.Engine.Simulations.PTDF.ptdf_driver import PTDF, PTDFOptions, PtdfGroupMode
from GridCal.Gui.GuiFunctions import ResultsModel


class PtdfTimeSeriesResults(PowerFlowResults):

    def __init__(self, n, m, nt, start, end, time_array=None):
        """
        TimeSeriesResults constructor
        @param n: number of buses
        @param m: number of branches
        @param nt: number of time steps
        """
        PowerFlowResults.__init__(self)
        self.name = 'PTDF Time series'
        self.nt = nt
        self.m = m
        self.n = n
        self.start = start
        self.end = end

        self.time = time_array

        if nt > 0:

            self.S = np.zeros((nt, n), dtype=complex)

            self.Sbranch = np.zeros((nt, m), dtype=complex)

            self.loading = np.zeros((nt, m), dtype=complex)

        else:
            self.S = None

            self.Sbranch = None

            self.loading = None

        self.available_results = [ResultTypes.BusActivePower,
                                  ResultTypes.BusReactivePower,
                                  ResultTypes.BranchPower,
                                  ResultTypes.BranchLoading]

    def set_at(self, t, results: PowerFlowResults):
        """
        Set the results at the step t
        @param t: time index
        @param results: PowerFlowResults instance
        """

        self.S[t, :] = results.Sbus

        self.Sbranch[t, :] = results.Sbranch

        self.loading[t, :] = results.loading

    def get_results_dict(self):
        """
        Returns a dictionary with the results sorted in a dictionary
        :return: dictionary of 2D numpy arrays (probably of complex numbers)
        """
        data = {'P': self.S.real.tolist(),
                'Q': self.S.imag.tolist(),
                'Sbr_real': self.Sbranch.real.tolist(),
                'Sbr_imag': self.Sbranch.imag.tolist(),
                'loading': np.abs(self.loading).tolist()}
        return data

    def save(self, fname):
        """
        Export as json
        """

        with open(fname, "wb") as output_file:
            json_str = json.dumps(self.get_results_dict())
            output_file.write(json_str)

    def mdl(self, result_type: ResultTypes, indices=None, names=None) -> "ResultsModel":
        """

        :param result_type:
        :param indices:
        :param names:
        :return:
        """

        if indices is None:
            indices = np.array(range(len(names)))

        if len(indices) > 0:

            labels = names[indices]

            if result_type == ResultTypes.BusActivePower:
                data = self.S[:, indices].real
                y_label = '(MW)'
                title = 'Bus active power '

            elif result_type == ResultTypes.BusReactivePower:
                data = self.S[:, indices].imag
                y_label = '(MVAr)'
                title = 'Bus reactive power '

            elif result_type == ResultTypes.BranchPower:
                data = self.Sbranch[:, indices]
                y_label = '(MVA)'
                title = 'Branch power '

            elif result_type == ResultTypes.BranchLoading:
                data = self.loading[:, indices] * 100
                y_label = '(%)'
                title = 'Branch loading '

            elif result_type == ResultTypes.BranchLosses:
                data = self.losses[:, indices]
                y_label = '(MVA)'
                title = 'Branch losses'

            elif result_type == ResultTypes.SimulationError:
                data = self.error.reshape(-1, 1)
                y_label = 'Per unit power'
                labels = [y_label]
                title = 'Error'

            else:
                raise Exception('Result type not understood:' + str(result_type))

            if self.time is not None:
                index = self.time
            else:
                index = list(range(data.shape[0]))

            # assemble model
            mdl = ResultsModel(data=data, index=index, columns=labels, title=title, ylabel=y_label)
            return mdl

        else:
            return None


class PtdfTimeSeries(QThread):
    progress_signal = Signal(float)
    progress_text = Signal(str)
    done_signal = Signal()
    name = 'PTDF Time Series'

    def __init__(self, grid: MultiCircuit, pf_options: PowerFlowOptions, start_=0, end_=None):
        """
        TimeSeries constructor
        @param grid: MultiCircuit instance
        @param pf_options: PowerFlowOptions instance
        """
        QThread.__init__(self)

        # reference the grid directly
        self.grid = grid

        self.pf_options = pf_options

        self.results = None

        self.start_ = start_

        self.end_ = end_

        self.elapsed = 0

        self.logger = Logger()

        self.__cancel__ = False

    def run_single_thread(self) -> PtdfTimeSeriesResults:
        """
        Run multi thread time series
        :return: TimeSeriesResults instance
        """

        # initialize the grid time series results, we will append the island results with another function
        n = len(self.grid.buses)
        m = len(self.grid.branches)
        nt = len(self.grid.time_profile)
        results = PtdfTimeSeriesResults(n, m, nt, self.start_, self.end_, time_array=self.grid.time_profile)

        if self.end_ is None:
            self.end_ = nt

        # if there are valid profiles...
        if self.grid.time_profile is not None:

            nc = self.grid.compile()

            options_ = PTDFOptions(group_mode=PtdfGroupMode.ByNode,
                                   power_increment=10,
                                   use_multi_threading=False)

            ptdf_driver = PTDF(grid=self.grid,
                               options=options_,
                               pf_options=self.pf_options)

            ptdf_driver.run()

            ptdf_df = ptdf_driver.results.get_results_data_frame()
            Pbus = nc.C_bus_gen * nc.generator_power_profile.real.T - nc.C_bus_load * nc.load_power_profile.real.T
            Pbus /= nc.Sbase

            # base magnitudes
            Pbr_0 = ptdf_driver.results.default_pf_results.Sbranch.real
            Pbus_0 = ptdf_driver.results.default_pf_results.Sbus.real

            for k, t_idx in enumerate(range(self.start_, self.end_)):
                # for i in range(nc.nbr):
                    # for j in range(nc.nbus):
                    #     dg = Pbus_0[j] - Pbus[j, t_idx]
                    #     results.Sbranch[k, i] = Pbr_0[i] + dg * ptdf_df.values[i, j]
                    #     results.loading[k, i] = results.Sbranch[k, i] / nc.br_rates[i]
                results.Sbranch[k, :] = Pbr_0 + np.dot(ptdf_df.values, (Pbus_0[:] - Pbus[:, t_idx]))
                results.loading[k, :] = results.Sbranch[k, :] / nc.br_rates

                progress = ((t_idx - self.start_ + 1) / (self.end_ - self.start_)) * 100
                self.progress_signal.emit(progress)
                self.progress_text.emit('Simulating PTDF at ' + str(self.grid.time_profile[t_idx]))

        else:
            print('There are no profiles')
            self.progress_text.emit('There are no profiles')

        return results

    def run(self):
        """
        Run the time series simulation
        @return:
        """
        self.__cancel__ = False
        a = time.time()

        self.results = self.run_single_thread()

        self.elapsed = time.time() - a

        # send the finnish signal
        self.progress_signal.emit(0.0)
        self.progress_text.emit('Done!')
        self.done_signal.emit()

    def cancel(self):
        """
        Cancel the simulation
        """
        self.__cancel__ = True
        self.pool.terminate()
        self.progress_signal.emit(0.0)
        self.progress_text.emit('Cancelled!')
        self.done_signal.emit()


if __name__ == '__main__':

    from GridCal.Engine import FileOpen, SolverType

    fname = r'C:\Users\PENVERSA\OneDrive - Red Eléctrica Corporación\Escritorio\IEEE cases\IEEE 30.gridcal'
    # fname = '/home/santi/Documentos/GitHub/GridCal/Grids_and_profiles/grids/IEEE39_1W.gridcal'
    # fname = '/home/santi/Documentos/GitHub/GridCal/Grids_and_profiles/grids/grid_2_islands.xlsx'
    # fname = '/home/santi/Documentos/GitHub/GridCal/Grids_and_profiles/grids/1354 Pegase.xlsx'

    main_circuit = FileOpen(fname).open()

    pf_options = PowerFlowOptions(solver_type=SolverType.NR)

    driver = PtdfTimeSeries(grid=main_circuit, pf_options=pf_options)

    driver.run()

    pass