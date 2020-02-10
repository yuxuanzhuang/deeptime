# This file is part of PyEMMA.
#
# Copyright (c) 2015, 2014 Computational Molecular Biology Group, Freie Universitaet Berlin (GER)
#
# PyEMMA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import warnings

import numpy as np

from sktime.base import Estimator
from sktime.markovprocess import MarkovStateModel, TransitionCountModel
from sktime.markovprocess.bhmm import discrete_hmm, init_discrete_hmm
from sktime.markovprocess.bhmm.init.discrete import init_discrete_hmm_spectral
from sktime.markovprocess.hmm import HiddenMarkovStateModel
from sktime.markovprocess.util import compute_dtrajs_effective
from sktime.util import ensure_dtraj_list


class MaximumLikelihoodHMSM(Estimator):

    def __init__(self, initial_model: HiddenMarkovStateModel, stride: int = 1, lagtime: int = 1, model=None):
        super().__init__(model=model)
        self.initial_transition_model = initial_model

    @property
    def n_hidden_states(self) -> int:
        return self._initial_model.n_hidden_states

    @property
    def initial_transition_model(self) -> HiddenMarkovStateModel:
        return self._initial_transition_model

    @initial_transition_model.setter
    def initial_transition_model(self, value: HiddenMarkovStateModel) -> None:
        self._initial_transition_model = value

    def fit(self, dtrajs, initial_model=None,**kwargs):
        if initial_model is None:
            initial_model = self.initial_transition_model
        if initial_model is None or not isinstance(initial_model, HiddenMarkovStateModel):
            raise ValueError("For estimation, an initial model of type "
                             "`sktime.markovprocess.hmm.HiddenMarkovStateModel` is required.")

        model = initial_model.copy()

        dtrajs = ensure_dtraj_list(dtrajs)
        dtrajs = compute_dtrajs_effective(dtrajs, lagtime=self.lagtime, n_states=initial_model.n_hidden_states,
                                          stride=self.stride)

        _maxT = max(len(obs) for obs in dtrajs)
        # pre-construct hidden variables
        N = self.n_states
        alpha = np.zeros((_maxT, N))
        beta = np.zeros((_maxT, N))
        pobs = np.zeros((_maxT, N))
        gammas = [np.zeros((len(obs), N)) for obs in dtrajs]
        count_matrices = [np.zeros((N, N)) for _ in dtrajs]

        it = 0
        likelihoods = np.empty(self.maxit)
        # flag if connectivity has changed (e.g. state lost) - in that case the likelihood
        # is discontinuous and can't be used as a convergence criterion in that iteration.
        tmatrix_nonzeros = model.transition_model.transition_matrix.nonzero()
        converged = False

        while not converged and it < self.maxit:
            loglik = 0.0
            for obs, gamma, counts in zip(dtrajs, gammas, count_matrices):
                loglik += self._forward_backward(model, obs, alpha, beta, gamma, pobs, counts)
            assert np.isfinite(loglik), it

            # convergence check
            if it > 0:
                dL = loglik - likelihoods[it-1]
                if dL < self._accuracy:
                    converged = True

            # update model
            self._update_model(model, dtrajs, gammas, count_matrices, maxiter=self._maxit_P)

            # connectivity change check
            tmatrix_nonzeros_new = model.transition_matrix.nonzero()
            if not np.array_equal(tmatrix_nonzeros, tmatrix_nonzeros_new):
                converged = False  # unset converged
                tmatrix_nonzeros = tmatrix_nonzeros_new

            # end of iteration
            likelihoods[it] = loglik
            it += 1

        likelihoods = np.resize(likelihoods, it)

        transition_counts = self._transition_counts(count_matrices)

        hmm_count_model = TransitionCountModel(count_matrix=transition_counts,
                                               lagtime=self.lagtime,
                                               physical_time=self.physical_time)

        model._likelihoods = likelihoods
        model._gammas = gammas
        model._initial_count = self._init_counts(gammas)
        model._hidden_state_trajectories = model.compute_viterbi_paths(dtrajs)

        self._model = model
        return self


class MaximumLikelihoodHMSM2(Estimator):
    """
    Maximum likelihood hidden markov state model estimator.
    """

    def __init__(self, n_states=2, lagtime=1, stride=1, msm_init='largest-strong', reversible=True, stationary=False,
                 connectivity=None, observe_nonempty=True, separate=None,
                 physical_time='1 step', accuracy=1e-3, maxit=1000):
        r"""Maximum likelihood estimator for a Hidden MSM given a MSM

        Parameters
        ----------
        n_states : int, optional, default=2
            number of hidden states
        lag : int, optional, default=1
            lagtime to fit the HMSM at
        stride : str or int, default=1
            stride between two lagged trajectories extracted from the input
            trajectories. Given trajectory s[t], stride and lag will result
            in trajectories
                s[0], s[lag], s[2 lag], ...
                s[stride], s[stride + lag], s[stride + 2 lag], ...
            Setting stride = 1 will result in using all data (useful for maximum
            likelihood estimator), while a Bayesian estimator requires a longer
            stride in order to have statistically uncorrelated trajectories.
            Setting stride = 'effective' uses the largest neglected timescale as
            an fit for the correlation time and sets the stride accordingly
        msm_init : str or :class:`MSM <sktime.markovprocess.MarkovStateModel>`
            MSM object to initialize the estimation, or one of following keywords:

            * 'largest-strong' or None (default) : Estimate MSM on the largest
                strongly connected set and use spectral clustering to generate an
                initial HMM
            * 'all' : Estimate MSM(s) on the full state space to initialize the
                HMM. This fit may be weakly connected or disconnected.
        reversible : bool, optional, default = True
            If true compute reversible MSM, else non-reversible MSM
        stationary : bool, optional, default=False
            If True, the initial distribution of hidden states is self-consistently computed as the stationary
            distribution of the transition matrix. If False, it will be estimated from the starting states.
            Only set this to true if you're sure that the observation trajectories are initiated from a global
            equilibrium distribution.
        connectivity : str, optional, default = None
            Defines if the resulting HMM will be defined on all hidden states or on
            a connected subset. Connectivity is defined by counting only
            transitions with at least mincount_connectivity counts.
            If a subset of states is used, all estimated quantities (transition
            matrix, stationary distribution, etc) are only defined on this subset
            and are correspondingly smaller than n_states.
            Following modes are available:

            * None or 'all' : The active set is the full set of states.
              Estimation is done on all weakly connected subsets separately. The
              resulting transition matrix may be disconnected.
            * 'largest' : The active set is the largest reversibly connected set.
            * 'populous' : The active set is the reversibly connected set with most counts.
        separate : None or iterable of int
            Force the given set of observed states to stay in a separate hidden state.
            The remaining n_states-1 states will be assigned by a metastable decomposition.
        observe_nonempty : bool
            If True, will restricted the observed states to the states that have
            at least one observation in the lagged input trajectories.
            If an initial MSM is given, this option is ignored and the observed
            subset is always identical to the active set of that MSM.
        physical_time : str, optional, default='1 step'
            Description of the physical time corresponding to the trajectory time
            step.  May be used by analysis algorithms such as plotting tools to
            pretty-print the axes. By default '1 step', i.e. there is no physical
            time unit. Specify by a number, whitespace and unit. Permitted units
            are (* is an arbitrary string):

            |  'fs',  'femtosecond*'
            |  'ps',  'picosecond*'
            |  'ns',  'nanosecond*'
            |  'us',  'microsecond*'
            |  'ms',  'millisecond*'
            |  's',   'second*'

        accuracy : float, optional, default = 1e-3
            convergence threshold for EM iteration. When two the likelihood does
            not increase by more than accuracy, the iteration is stopped
            successfully.
        maxit : int, optional, default = 1000
            stopping criterion for EM iteration. When so many iterations are
            performed without reaching the requested accuracy, the iteration is
            stopped without convergence (a warning is given)

        """
        super(MaximumLikelihoodHMSM2, self).__init__()
        self.n_hidden_states = n_states
        self.lagtime = lagtime
        self.stride = stride
        self.msm_init = msm_init
        self.reversible = reversible
        self.stationary = stationary
        self.connectivity = connectivity
        self.separate = separate
        self.observe_nonempty = observe_nonempty
        self.physical_time = physical_time
        self.accuracy = accuracy
        self.maxit = maxit

    def fetch_model(self) -> HiddenMarkovStateModel:
        return self._model

    @staticmethod
    def initial_guess(dtrajs, lagtime, n_hidden_states, stride) -> HiddenMarkovStateModel:
        dtrajs = ensure_dtraj_list(dtrajs)
        dtrajs_lagged_strided = compute_dtrajs_effective(dtrajs, lagtime=lagtime,
                                                         n_states=n_hidden_states,
                                                         stride=stride)



    def fit(self, dtrajs, **kwargs):
        dtrajs = ensure_dtraj_list(dtrajs)
        # CHECK LAG
        trajlengths = [len(dtraj) for dtraj in dtrajs]
        if self.lagtime >= np.max(trajlengths):
            raise ValueError(f'Illegal lag time {self.lagtime}, needs to be smaller than longest input trajectory.')
        if self.lagtime > np.mean(trajlengths):
            warnings.warn(f'Lag time {self.lagtime} is on the order of mean trajectory length '
                          f'{np.mean(trajlengths)}. It is recommended to fit at least four lag times in each '
                          'trajectory. HMM might be inaccurate.')

        dtrajs_lagged_strided = compute_dtrajs_effective(dtrajs, lagtime=self.lagtime,
                                                         n_states=self.n_hidden_states,
                                                         stride=self.stride)

        # INIT HMM
        if isinstance(self.msm_init, str):
            args = dict(observations=dtrajs_lagged_strided, n_states=self.n_hidden_states, lag=1,
                        reversible=self.reversible, stationary=True, regularize=True,
                        separate=self.separate)
            if self.msm_init == 'largest-strong':
                args['method'] = 'lcs-spectral'
            elif self.msm_init == 'all':
                args['method'] = 'spectral'

            hmm_init = init_discrete_hmm(**args)
        elif isinstance(self.msm_init, MarkovStateModel):
            msm_count_model = self.msm_init.count_model
            # pcca = self.msm_init.pcca(n_metastable_sets=self.n_hidden_states)

            p0, P0, pobs0 = init_discrete_hmm_spectral(msm_count_model.count_matrix.toarray(),
                                                       self.n_hidden_states, reversible=self.reversible,
                                                       stationary=True, P=self.msm_init.transition_matrix,
                                                       separate=self.separate)
            hmm_init = discrete_hmm(p0, P0, pobs0)
        else:
            raise RuntimeError("msm init was neither a string (largest-strong or spectral) nor "
                               "a MarkovStateModel: {}".format(self.msm_init))

        # ---------------------------------------------------------------------------------------
        # Estimate discrete HMM
        # ---------------------------------------------------------------------------------------
        from sktime.markovprocess.bhmm.estimators.maximum_likelihood import MaximumLikelihoodHMM
        hmm_est = MaximumLikelihoodHMM(self.n_hidden_states, initial_model=hmm_init,
                                       output='discrete', reversible=self.reversible, stationary=self.stationary,
                                       accuracy=self.accuracy, maxit=self.maxit)
        hmm = hmm_est.fit(dtrajs_lagged_strided).fetch_model()
        # observation_state_symbols = np.unique(np.concatenate(dtrajs_lagged_strided))
        # update the count matrix from the counts obtained via the Viterbi paths.
        hmm_count_model = TransitionCountModel(count_matrix=hmm.transition_counts,
                                               lagtime=self.lagtime,
                                               physical_time=self.physical_time)
        # set model parameters
        self._model = HiddenMarkovStateModel(transition_matrix=hmm.transition_matrix,
                                             observation_probabilities=hmm.output_model.output_probabilities,
                                             stride=self.stride,
                                             stationary_distribution=hmm.stationary_distribution,
                                             initial_counts=hmm.initial_count,
                                             reversible=self.reversible,
                                             initial_distribution=hmm.initial_distribution, count_model=hmm_count_model,
                                             bhmm_model=hmm,
                                             observation_state_symbols=None)
        return self

    @property
    def msm_init(self):
        """ MSM initialization method, should be one of:
        * instance of :class:`MSM <sktime.markovprocess.MarkovStateModel>`

        or a string:

        * 'largest-strong' or None (default) : Estimate MSM on the largest
            strongly connected set and use spectral clustering to generate an
            initial HMM
        * 'all' : Estimate MSM(s) on the full state space to initialize the
            HMM. This fit maybe weakly connected or disconnected.
        """
        return self._msm_init

    @msm_init.setter
    def msm_init(self, value: [str, MarkovStateModel]):
        if isinstance(value, MarkovStateModel) and value.count_model is None:
            raise NotImplementedError('Requires markov state model instance that contains a count model '
                                      'with count matrix for estimation.')
        elif isinstance(value, str):
            supported = ('largest-strong', 'all')
            if value not in supported:
                raise NotImplementedError(f'unknown msm_init value, was "{value}",'
                                          f'but valid options are {supported}.')
        self._msm_init = value

    @property
    def connectivity(self):
        return self._connectivity

    @connectivity.setter
    def connectivity(self, value):
        allowed = (None, 'largest', 'populous')
        if value not in allowed:
            raise ValueError(f'Illegal value for connectivity: {value}. Allowed values are one of: {allowed}.')
        self._connectivity = value

    # TODO: model attribute
    def compute_trajectory_weights(self, dtrajs_observed):
        r"""Uses the HMSM to assign a probability weight to each trajectory frame.

        This is a powerful function for the calculation of arbitrary observables in the trajectories one has
        started the analysis with. The stationary probability of the MSM will be used to reweigh all states.
        Returns a list of weight arrays, one for each trajectory, and with a number of elements equal to
        trajectory frames. Given :math:`N` trajectories of lengths :math:`T_1` to :math:`T_N`, this function
        returns corresponding weights:

        .. math::

            (w_{1,1}, ..., w_{1,T_1}), (w_{N,1}, ..., w_{N,T_N})

        that are normalized to one:

        .. math::

            \sum_{i=1}^N \sum_{t=1}^{T_i} w_{i,t} = 1

        Suppose you are interested in computing the expectation value of a function :math:`a(x)`, where :math:`x`
        are your input configurations. Use this function to compute the weights of all input configurations and
        obtain the estimated expectation by:

        .. math::

            \langle a \rangle = \sum_{i=1}^N \sum_{t=1}^{T_i} w_{i,t} a(x_{i,t})

        Or if you are interested in computing the time-lagged correlation between functions :math:`a(x)` and
        :math:`b(x)` you could do:

        .. math::

            \langle a(t) b(t+\tau) \rangle_t = \sum_{i=1}^N \sum_{t=1}^{T_i} w_{i,t} a(x_{i,t}) a(x_{i,t+\tau})

        Returns
        -------
        The normalized trajectory weights. Given :math:`N` trajectories of lengths :math:`T_1` to :math:`T_N`,
        returns the corresponding weights:

        .. math::

            (w_{1,1}, ..., w_{1,T_1}), (w_{N,1}, ..., w_{N,T_N})

        """
        # compute stationary distribution, expanded to full set
        statdist = self.stationary_distribution_obs
        statdist = np.append(statdist, [-1])  # add a zero weight at index -1, to deal with unobserved states
        # histogram observed states
        import msmtools.dtraj as msmtraj
        hist = 1.0 * msmtraj.count_states(dtrajs_observed, ignore_negative=True)
        # simply read off stationary distribution and accumulate total weight
        W = []
        wtot = 0.0
        for dtraj in self.discrete_trajectories_obs:
            w = statdist[dtraj] / hist[dtraj]
            W.append(w)
            wtot += np.sum(w)
        # normalize
        for w in W:
            w /= wtot
        # done
        return W

    ################################################################################
    # Generation of trajectories and samples
    ################################################################################

    # TODO: generate_traj. How should that be defined? Probably indexes of observable states, but should we specify
    #                      hidden or observable states as start and stop states?
    # TODO: sample_by_state. How should that be defined?

    def sample_by_observation_probabilities(self, nsample):
        r"""Generates samples according to the current observation probability distribution

        Parameters
        ----------
        nsample : int
            Number of samples per distribution. If replace = False, the number of returned samples per state could be
            smaller if less than nsample indexes are available for a state.

        Returns
        -------
        indexes : length m list of ndarray( (nsample, 2) )
            List of the sampled indices by distribution.
            Each element is an index array with a number of rows equal to nsample, with rows consisting of a
            tuple (i, t), where i is the index of the trajectory and t is the time index within the trajectory.

        """
        from msmtools.dtraj import sample_indexes_by_distribution
        return sample_indexes_by_distribution(self.observable_state_indexes, self.observation_probabilities, nsample)
