from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()

import copy
from logging import getLogger
import os

import chainer
from chainer import functions as F
from chainer import serializers
import numpy as np

from chainerrl import agent
from chainerrl.misc import async
from chainerrl.misc import copy_param
from chainerrl.misc.makedirs import makedirs
from chainerrl.recurrent import Recurrent
from chainerrl.recurrent import RecurrentChainMixin
from chainerrl.recurrent import state_kept

logger = getLogger(__name__)


class A3CModel(chainer.Link):
    """A3C model."""

    def pi_and_v(self, obs):
        """Evaluate the policy and the V-function.

        Args:
            obs (Variable or ndarray): Batched observations.
        Returns:
            Distribution and Variable
        """
        raise NotImplementedError()


class A3CSeparateModel(chainer.Chain, A3CModel, RecurrentChainMixin):
    """A3C model that consists of a separate policy and V-function.

    Args:
        pi (Policy): Policy.
        v (VFunction): V-function.
    """

    def __init__(self, pi, v):
        super().__init__(pi=pi, v=v)

    def pi_and_v(self, obs):
        pout = self.pi(obs)
        vout = self.v(obs)
        return pout, vout


class A3CSharedModel(chainer.Chain, A3CModel, RecurrentChainMixin):
    """A3C model where the policy and V-function share parameters.

    Args:
        shared (Link): Shared part. Nonlinearity must be included in it.
        pi (Policy): Policy that receives output of shared as input.
        v (VFunction): V-function that receives output of shared as input.
    """

    def __init__(self, shared, pi, v):
        super().__init__(shared=shared, pi=pi, v=v)

    def pi_and_v(self, obs):
        h = self.shared(obs)
        pout = self.pi(h)
        vout = self.v(h)
        return pout, vout


class A3C(agent.Agent):
    """A3C: Asynchronous Advantage Actor-Critic.

    See http://arxiv.org/abs/1602.01783
    """

    def __init__(self, model, optimizer, t_max, gamma, beta=1e-2,
                 process_idx=0, clip_reward=True, phi=lambda x: x,
                 pi_loss_coef=1.0, v_loss_coef=0.5,
                 keep_loss_scale_same=False,
                 normalize_grad_by_t_max=False,
                 use_average_reward=False, average_reward_tau=1e-2,
                 act_deterministically=False):

        assert isinstance(model, A3CModel)
        # Globally shared model
        self.shared_model = model

        # Thread specific model
        self.model = copy.deepcopy(self.shared_model)
        async.assert_params_not_shared(self.shared_model, self.model)

        self.optimizer = optimizer

        self.t_max = t_max
        self.gamma = gamma
        self.beta = beta
        self.process_idx = process_idx
        self.clip_reward = clip_reward
        self.phi = phi
        self.pi_loss_coef = pi_loss_coef
        self.v_loss_coef = v_loss_coef
        self.keep_loss_scale_same = keep_loss_scale_same
        self.normalize_grad_by_t_max = normalize_grad_by_t_max
        self.use_average_reward = use_average_reward
        self.average_reward_tau = average_reward_tau
        self.act_deterministically = act_deterministically

        self.t = 0
        self.t_start = 0
        self.past_action_log_prob = {}
        self.past_action_entropy = {}
        self.past_states = {}
        self.past_rewards = {}
        self.past_values = {}
        self.average_reward = 0
        # A3C won't use a explorer, but this arrtibute is referenced by run_dqn
        self.explorer = None

    def sync_parameters(self):
        copy_param.copy_param(target_link=self.model,
                              source_link=self.shared_model)

    @property
    def shared_attributes(self):
        return ('shared_model', 'optimizer')

    def update(self, statevar):
        assert self.t_start < self.t

        if statevar is None:
            R = 0
        else:
            with state_kept(self.model):
                _, vout = self.model.pi_and_v(statevar)
            R = float(vout.data)

        pi_loss = 0
        v_loss = 0
        for i in reversed(range(self.t_start, self.t)):
            R *= self.gamma
            R += self.past_rewards[i]
            if self.use_average_reward:
                R -= self.average_reward
            v = self.past_values[i]
            if self.process_idx == 0:
                logger.debug('t:%s s:%s v:%s R:%s',
                             i, self.past_states[i].data.sum(), v.data, R)
            advantage = R - v
            if self.use_average_reward:
                self.average_reward += self.average_reward_tau * \
                    float(advantage.data)
            # Accumulate gradients of policy
            log_prob = self.past_action_log_prob[i]
            entropy = self.past_action_entropy[i]

            # Log probability is increased proportionally to advantage
            pi_loss -= log_prob * float(advantage.data)
            # Entropy is maximized
            pi_loss -= self.beta * entropy
            # Accumulate gradients of value function

            v_loss += (v - R) ** 2 / 2

        if self.pi_loss_coef != 1.0:
            pi_loss *= self.pi_loss_coef

        if self.v_loss_coef != 1.0:
            v_loss *= self.v_loss_coef

        # Normalize the loss of sequences truncated by terminal states
        if self.keep_loss_scale_same and \
                self.t - self.t_start < self.t_max:
            factor = self.t_max / (self.t - self.t_start)
            pi_loss *= factor
            v_loss *= factor

        if self.normalize_grad_by_t_max:
            pi_loss /= self.t - self.t_start
            v_loss /= self.t - self.t_start

        if self.process_idx == 0:
            logger.debug('pi_loss:%s v_loss:%s', pi_loss.data, v_loss.data)

        total_loss = pi_loss + F.reshape(v_loss, pi_loss.data.shape)

        # Compute gradients using thread-specific model
        self.model.zerograds()
        total_loss.backward()
        # Copy the gradients to the globally shared model
        self.shared_model.zerograds()
        copy_param.copy_grad(
            target_link=self.shared_model, source_link=self.model)
        # Update the globally shared model
        if self.process_idx == 0:
            norm = self.optimizer.compute_grads_norm()
            logger.debug('grad norm:%s', norm)
        self.optimizer.update()
        if self.process_idx == 0:
            logger.debug('update')

        self.sync_parameters()
        if isinstance(self.model, Recurrent):
            self.model.unchain_backward()

        self.past_action_log_prob = {}
        self.past_action_entropy = {}
        self.past_states = {}
        self.past_rewards = {}
        self.past_values = {}

        self.t_start = self.t

    def act_and_train(self, state, reward):

        if self.clip_reward:
            reward = np.clip(reward, -1, 1)

        statevar = chainer.Variable(np.expand_dims(self.phi(state), 0))

        self.past_rewards[self.t - 1] = reward

        if self.t - self.t_start == self.t_max:
            self.update(statevar)

        self.past_states[self.t] = statevar
        pout, vout = self.model.pi_and_v(statevar)
        action = pout.sample()
        action.creator = None  # Do not backprop through sampled actions
        self.past_action_log_prob[self.t] = pout.log_prob(action)
        self.past_action_entropy[self.t] = pout.entropy
        self.past_values[self.t] = vout
        self.t += 1
        action = action.data[0]
        if self.process_idx == 0:
            logger.debug('t:%s r:%s a:%s pout:%s',
                         self.t, reward, action, pout)
        return action

    def act(self, obs):
        with chainer.no_backprop_mode():
            statevar = np.expand_dims(self.phi(obs), 0)
            pout, _ = self.model.pi_and_v(statevar)
            if self.act_deterministically:
                return pout.most_probable.data[0]
            else:
                return pout.sample().data[0]

    def stop_episode_and_train(self, state, reward, done=False):
        if self.clip_reward:
            reward = np.clip(reward, -1, 1)

        self.past_rewards[self.t - 1] = reward
        if done:
            self.update(None)
        else:
            statevar = chainer.Variable(np.expand_dims(self.phi(state), 0))
            self.update(statevar)

        if isinstance(self.model, Recurrent):
            self.model.reset_state()

    def stop_episode(self):
        if isinstance(self.model, Recurrent):
            self.model.reset_state()

    def save(self, dirname):
        makedirs(dirname, exist_ok=True)
        serializers.save_npz(os.path.join(dirname, 'model.npz'), self.model)
        serializers.save_npz(
            os.path.join(dirname, 'optimizer.npz'), self.optimizer)

    def load(self, dirname):
        serializers.load_npz(os.path.join(dirname, 'model.npz'), self.model)
        copy_param.copy_param(target_link=self.shared_model,
                              source_link=self.model)

        opt_filename = os.path.join(dirname, 'optimizer.npz')
        if os.path.exists(opt_filename):
            serializers.load_npz(opt_filename, self.optimizer)
        else:
            print('WARNING: {0} was not found, so loaded only a model'.format(
                opt_filename))

    def get_stats_keys(self):
        return ()

    def get_stats_values(self):
        return ()
