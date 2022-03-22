"""Behavioural Cloning (BC).

Trains policy by applying supervised learning to a fixed dataset of (observation,
action) pairs generated by some expert demonstrator.
"""

import dataclasses
import itertools
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Tuple, Type, Union

import gym
import numpy as np
import torch as th
import tqdm
from stable_baselines3.common import policies, utils, vec_env

from imitation.algorithms import base as algo_base
from imitation.data import rollout, types
from imitation.policies import base as policy_base
from imitation.util import logger as imit_logger


@dataclasses.dataclass(frozen=True)
class BatchIteratorWithEpochEndCallback:
    """Loops through batches from a batch loader and calls a callback after every epoch.

    Will throw an exception when an epoch contains no batches.
    """

    batch_loader: Iterable[algo_base.TransitionMapping]
    n_epochs: Optional[int]
    n_batches: Optional[int]
    on_epoch_end: Optional[Callable[[int], None]]

    def __post_init__(self):
        epochs_and_batches_specified = (
            self.n_epochs is not None and self.n_batches is not None
        )
        neither_epochs_nor_batches_specified = (
            self.n_epochs is None and self.n_batches is None
        )
        if epochs_and_batches_specified or neither_epochs_nor_batches_specified:
            raise ValueError(
                "Must provide exactly one of `n_epochs` and `n_batches` arguments.",
            )

    def __iter__(self) -> Iterator[algo_base.TransitionMapping]:
        def batch_iterator():

            # Note: the islice here ensures we do not exceed self.n_epochs
            for epoch_num in itertools.islice(itertools.count(), self.n_epochs):
                num_batches_in_epoch = 0
                for num_batches_in_epoch, batch in enumerate(self.batch_loader):
                    yield batch

                if num_batches_in_epoch == 0:
                    raise AssertionError(
                        f"Data loader returned no data after "
                        f"{num_batches_in_epoch} batches, during epoch "
                        f"{epoch_num} -- did it reset correctly?",
                    )
                if self.on_epoch_end is not None:
                    self.on_epoch_end(epoch_num)

        # Note: the islice here ensures we do not exceed self.n_batches
        return itertools.islice(batch_iterator(), self.n_batches)


@dataclasses.dataclass(frozen=True)
class BehaviorCloningLoss:
    """Container for the different components of behavior cloning loss."""

    neglogp: th.Tensor
    entropy: th.Tensor
    ent_loss: th.Tensor
    prob_true_act: th.Tensor
    l2_norm: th.Tensor
    l2_loss: th.Tensor
    loss: th.Tensor


@dataclasses.dataclass(frozen=True)
class BehaviorCloningLossCalculator:
    """Functor to compute the loss used in Behavior Cloning."""

    ent_weight: float
    l2_weight: float

    def __call__(
        self,
        policy: policies.ActorCriticPolicy,
        obs: Union[th.Tensor, np.ndarray],
        acts: Union[th.Tensor, np.ndarray],
    ) -> BehaviorCloningLoss:
        """Calculate the supervised learning loss used to train the behavioral clone.

        Args:
            policy: The actor-critic policy of which to compute the loss.
            obs: The observations seen by the expert.
            acts: The actions taken by the expert.
        """
        _, log_prob, entropy = policy.evaluate_actions(obs, acts)
        prob_true_act = th.exp(log_prob).mean()
        log_prob = log_prob.mean()
        entropy = entropy.mean()

        l2_norms = [th.sum(th.square(w)) for w in policy.parameters()]
        l2_norm = sum(l2_norms) / 2  # divide by 2 to cancel with gradient of square

        ent_loss = -self.ent_weight * entropy
        neglogp = -log_prob
        l2_loss = self.l2_weight * l2_norm
        loss = neglogp + ent_loss + l2_loss

        return BehaviorCloningLoss(
            neglogp=neglogp,
            entropy=entropy,
            ent_loss=ent_loss,
            prob_true_act=prob_true_act,
            l2_norm=l2_norm,
            l2_loss=l2_loss,
            loss=loss,
        )


@dataclasses.dataclass(frozen=True)
class BehaviorCloningTrainer:
    """Functor to fit a policy to expert demonstration data."""

    loss: BehaviorCloningLossCalculator
    optimizer: th.optim.Optimizer
    policy: policies.ActorCriticPolicy
    device: th.device  # TODO(max): not sure whether the device belongs in this class

    def __call__(self, batch) -> BehaviorCloningLoss:
        obs = th.as_tensor(batch["obs"], device=self.device).detach()
        acts = th.as_tensor(batch["acts"], device=self.device).detach()
        bc_loss = self.loss(self.policy, obs, acts)

        self.optimizer.zero_grad()
        bc_loss.loss.backward()
        self.optimizer.step()

        return bc_loss


def enumerate_batches(
    batch_it: Iterable[algo_base.TransitionMapping],
) -> Iterable[Tuple[Tuple[int, int, int], algo_base.TransitionMapping]]:
    """Prepends batch stats before the batches of a batch iterator."""
    num_samples_so_far = 0
    for num_batches, batch in enumerate(batch_it):
        batch_size = len(batch["obs"])
        num_samples_so_far += batch_size
        yield (num_batches, batch_size, num_samples_so_far), batch


@dataclasses.dataclass(frozen=True)
class RolloutStatsComputer:
    """Computes statistics about rollouts.

    Args:
        venv: The vectorized environment in which to compute the rollouts.
        n_episodes: The number of episodes to base the statistics on.
    """

    venv: vec_env.VecEnv
    n_episodes: int

    # TODO(shwang): Maybe instead use a callback that can be shared between
    #   all algorithms' `.train()` for generating rollout stats.
    #   EvalCallback could be a good fit:
    #   https://stable-baselines3.readthedocs.io/en/master/guide/callbacks.html#evalcallback

    def __call__(self, policy: policies.ActorCriticPolicy) -> Mapping[str, float]:
        if self.venv is not None and self.n_episodes > 0:
            trajs = rollout.generate_trajectories(
                policy,
                self.venv,
                rollout.make_min_episodes(self.n_episodes),
            )
            return rollout.rollout_stats(trajs)
        else:
            return dict()


class BCLogger:
    """Utility class to help logging information relevant to Behavior Cloning."""

    def __init__(self, logger: imit_logger.HierarchicalLogger):
        """Create new BC logger.

        Args:
            logger: The logger to which to feed all the information.
        """
        self._logger = logger
        self._tensorboard_step = 0

    def reset_tensorboard_steps(self):
        self._tensorboard_step = 0

    def log_epoch(self, epoch_number):
        self._logger.record("bc/epoch", epoch_number)

    def log_batch(
        self,
        batch_num: int,
        batch_size: int,
        num_samples_so_far: int,
        loss: BehaviorCloningLoss,
        rollout_stats: Mapping[str, float],
    ):
        self._tensorboard_step += 1

        self._logger.record("batch_size", batch_size)
        self._logger.record("bc/batch", batch_num)
        self._logger.record("bc/samples_so_far", num_samples_so_far)
        for k, v in loss.__dict__.items():
            self._logger.record(f"bc/{k}", v)

        for k, v in rollout_stats.items():
            if "return" in k and "monitor" not in k:
                self._logger.record("rollout/" + k, v)
        self._logger.dump(self._tensorboard_step)

    def __getstate__(self):
        state = self.__dict__.copy()
        del state["_logger"]
        return state


def reconstruct_policy(
    policy_path: str,
    device: Union[th.device, str] = "auto",
) -> policies.ActorCriticPolicy:
    """Reconstruct a saved policy.

    Args:
        policy_path: path where `.save_policy()` has been run.
        device: device on which to load the policy.

    Returns:
        policy: policy with reloaded weights.
    """
    policy = th.load(policy_path, map_location=utils.get_device(device))
    assert isinstance(policy, policies.ActorCriticPolicy)
    return policy


class BC(algo_base.DemonstrationAlgorithm):
    """Behavioral cloning (BC).

    Recovers a policy via supervised learning from observation-action pairs.
    """

    def __init__(
        self,
        *,
        observation_space: gym.Space,
        action_space: gym.Space,
        policy: Optional[policies.ActorCriticPolicy] = None,
        demonstrations: Optional[algo_base.AnyTransitions] = None,
        batch_size: int = 32,
        optimizer_cls: Type[th.optim.Optimizer] = th.optim.Adam,
        optimizer_kwargs: Optional[Mapping[str, Any]] = None,
        ent_weight: float = 1e-3,
        l2_weight: float = 0.0,
        device: Union[str, th.device] = "auto",
        custom_logger: Optional[imit_logger.HierarchicalLogger] = None,
    ):
        """Builds BC.

        Args:
            observation_space: the observation space of the environment.
            action_space: the action space of the environment.
            policy: a Stable Baselines3 policy; if unspecified,
                defaults to `FeedForward32Policy`.
            demonstrations: Demonstrations from an expert (optional). Transitions
                expressed directly as a `types.TransitionsMinimal` object, a sequence
                of trajectories, or an iterable of transition batches (mappings from
                keywords to arrays containing observations, etc).
            batch_size: The number of samples in each batch of expert data.
            optimizer_cls: optimiser to use for supervised training.
            optimizer_kwargs: keyword arguments, excluding learning rate and
                weight decay, for optimiser construction.
            ent_weight: scaling applied to the policy's entropy regularization.
            l2_weight: scaling applied to the policy's L2 regularization.
            device: name/identity of device to place policy on.
            custom_logger: Where to log to; if None (default), creates a new logger.

        Raises:
            ValueError: If `weight_decay` is specified in `optimizer_kwargs` (use the
                parameter `l2_weight` instead.)
        """
        self._demo_data_loader: Optional[Iterable[algo_base.TransitionMapping]] = None
        self.batch_size = batch_size
        super().__init__(
            demonstrations=demonstrations,
            custom_logger=custom_logger,
        )
        self._bc_logger = BCLogger(self.logger)

        self.action_space = action_space
        self.observation_space = observation_space
        self.device = utils.get_device(device)

        if policy is None:
            policy = policy_base.FeedForward32Policy(
                observation_space=observation_space,
                action_space=action_space,
                # Set lr_schedule to max value to force error if policy.optimizer
                # is used by mistake (should use self.optimizer instead).
                lr_schedule=lambda _: th.finfo(th.float32).max,
            )
        self._policy = policy.to(self.device)
        # TODO(adam): make policy mandatory and delete observation/action space params?
        assert self.policy.observation_space == self.observation_space
        assert self.policy.action_space == self.action_space

        if optimizer_kwargs:
            if "weight_decay" in optimizer_kwargs:
                raise ValueError("Use the parameter l2_weight instead of weight_decay.")
        optimizer_kwargs = optimizer_kwargs or {}
        optimizer = optimizer_cls(
            self.policy.parameters(),
            **optimizer_kwargs,
        )
        loss_computer = BehaviorCloningLossCalculator(ent_weight, l2_weight)
        self.trainer = BehaviorCloningTrainer(
            loss_computer,
            optimizer,
            policy,
            self.device,
        )

    @property
    def policy(self) -> policies.ActorCriticPolicy:
        return self._policy

    def set_demonstrations(self, demonstrations: algo_base.AnyTransitions) -> None:
        self._demo_data_loader = algo_base.make_data_loader(
            demonstrations,
            self.batch_size,
        )

    def train(
        self,
        *,
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        on_epoch_end: Optional[Callable[[], None]] = None,
        on_batch_end: Optional[Callable[[], None]] = None,
        log_interval: int = 500,
        log_rollouts_venv: Optional[vec_env.VecEnv] = None,
        log_rollouts_n_episodes: int = 5,
        progress_bar: bool = True,
        reset_tensorboard: bool = False,
    ):
        """Train with supervised learning for some number of epochs.

        Here an 'epoch' is just a complete pass through the expert data loader,
        as set by `self.set_expert_data_loader()`.

        Args:
            n_epochs: Number of complete passes made through expert data before ending
                training. Provide exactly one of `n_epochs` and `n_batches`.
            n_batches: Number of batches loaded from dataset before ending training.
                Provide exactly one of `n_epochs` and `n_batches`.
            on_epoch_end: Optional callback with no parameters to run at the end of each
                epoch.
            on_batch_end: Optional callback with no parameters to run at the end of each
                batch.
            log_interval: Log stats after every log_interval batches.
            log_rollouts_venv: If not None, then this VecEnv (whose observation and
                actions spaces must match `self.observation_space` and
                `self.action_space`) is used to generate rollout stats, including
                average return and average episode length. If None, then no rollouts
                are generated.
            log_rollouts_n_episodes: Number of rollouts to generate when calculating
                rollout stats. Non-positive number disables rollouts.
            progress_bar: If True, then show a progress bar during training.
            reset_tensorboard: If True, then start plotting to Tensorboard from x=0
                even if `.train()` logged to Tensorboard previously. Has no practical
                effect if `.train()` is being called for the first time.
        """
        if reset_tensorboard:
            self._bc_logger.reset_tensorboard_steps()

        compute_rollout_stats = RolloutStatsComputer(
            log_rollouts_venv,
            log_rollouts_n_episodes,
        )

        def _on_epoch_end(epoch_number: int):
            if isinstance(batches_with_stats, tqdm.tqdm):
                total_num_epochs_str = f"of {n_epochs}" if n_epochs is not None else ""
                batches_with_stats.display(
                    f"Epoch {epoch_number} {total_num_epochs_str}",
                    pos=1,
                )
            self._bc_logger.log_epoch(epoch_number)
            if on_epoch_end is not None:
                on_epoch_end()

        demonstration_batches = BatchIteratorWithEpochEndCallback(
            self._demo_data_loader,
            n_epochs,
            n_batches,
            _on_epoch_end,
        )
        batches_with_stats = enumerate_batches(demonstration_batches)

        if progress_bar:
            batches_with_stats = tqdm.tqdm(
                batches_with_stats,
                unit="batch",
                total=n_batches,
            )

        for ((batch_num, batch_size, num_samples_so_far), batch) in batches_with_stats:
            loss = self.trainer(batch)

            if batch_num % log_interval == 0:
                rollout_stats = compute_rollout_stats(self.policy)

                self._bc_logger.log_batch(
                    batch_num,
                    batch_size,
                    num_samples_so_far,
                    loss,
                    rollout_stats,
                )

            if on_batch_end is not None:
                on_batch_end()

    def save_policy(self, policy_path: types.AnyPath) -> None:
        """Save policy to a path. Can be reloaded by `.reconstruct_policy()`.

        Args:
            policy_path: path to save policy to.
        """
        th.save(self.policy, policy_path)
