# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Functional MCMC: A functional API for creating new Markov Chains.

The core convention of this API is that transition operators have the following
form:

```
transition_operator(state...) -> (new_state..., extra_outputs)
```

Where 'x...', reresents one or more values. This operator can then be called
recursively as follows:

```
state = ...
while not_done:
  state, extra = transition_operator(*state)
```

`state` is allowed to be partially specified (i.e. have `None` elements), which
the transition operator must impute when it returns the new state.
"""

from __future__ import absolute_import
from __future__ import division
# [internal] enable type annotations
from __future__ import print_function

import collections

import numpy as np
import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp
from typing import Any, Callable, Mapping, Optional, Sequence, Text, Tuple, Union
from tensorflow.python.util import nest  # pylint: disable=g-direct-tensorflow-import

tfb = tfp.bijectors
mcmc_util = tfp.mcmc.internal.util

__all__ = [
    'adam_init',
    'adam_step',
    'AdamExtra',
    'AdamState',
    'blanes_3_stage_step',
    'blanes_4_stage_step',
    'call_and_grads',
    'call_fn',
    'gaussian_momentum_sample',
    'hamiltonian_integrator',
    'hamiltonian_monte_carlo',
    'hamiltonian_monte_carlo_init',
    'HamiltonianMonteCarloExtra',
    'HamiltonianMonteCarloState',
    'IntegratorExtras',
    'IntegratorState',
    'IntegratorStep',
    'IntegratorStepState',
    'leapfrog_step',
    'make_gaussian_kinetic_energy_fn',
    'maybe_broadcast_structure',
    'metropolis_hastings_step',
    'PotentialFn',
    'ruth4_step',
    'sign_adaptation',
    'State',
    'symmetric_spliting_integrator_step',
    'trace',
    'transform_log_prob_fn',
    'transition_kernel_wrapper',
    'TransitionOperator',
]

AnyTensor = Union[tf.Tensor, np.ndarray, np.generic]
IntTensor = Union[int, tf.Tensor, np.ndarray, np.integer]
FloatTensor = Union[float, tf.Tensor, np.ndarray, np.floating]
# TODO(b/109648354): Correctly represent the recursive nature of this type.
TensorNest = Union[AnyTensor, Sequence[AnyTensor], Mapping[Any, AnyTensor]]
TensorSpecNest = Union[tf.TensorSpec, Sequence[tf.TensorSpec],
                       Mapping[Any, tf.TensorSpec]]
BijectorNest = Union[tfb.Bijector, Sequence[tfb.Bijector], Mapping[Any, tfb
                                                                   .Bijector]]
FloatNest = Union[FloatTensor, Sequence[FloatTensor], Mapping[Any, FloatTensor]]
State = TensorNest  # pylint: disable=invalid-name
TransitionOperator = Callable[..., Tuple[State, TensorNest]]
PotentialFn = Union[Callable[[TensorNest], Tuple[tf.Tensor, TensorNest]],
                    Callable[..., Tuple[tf.Tensor, TensorNest]]]


def trace(
    state: State,
    fn: TransitionOperator,
    num_steps: IntTensor,
    trace_fn: Callable[[State, TensorNest], TensorNest],
    parallel_iterations: int = 10,
) -> Tuple[State, TensorNest]:
  """`TransitionOperator` that runs `fn` repeatedly and traces its outputs.

  Args:
    state: A nest of `Tensor`s or None.
    fn: A `TransitionOperator`.
    num_steps: Number of steps to run the function for. Must be greater than 1.
    trace_fn: Callable that the unpacked outputs of `fn` and returns a nest of
      `Tensor`s. These will be stacked and returned.
    parallel_iterations: Number of iterations of the while loop to run in
      parallel.

  Returns:
    state: The final state returned by `fn`.
    traces: Stacked outputs of `trace_fn`.
  """
  state = tf.nest.map_structure(
      lambda t: t if t is None else tf.convert_to_tensor(t), state)

  def wrapper(state):
    state, extra = tf.nest.map_structure(tf.convert_to_tensor,
                                         call_fn(fn, state))
    trace_element = tf.nest.map_structure(tf.convert_to_tensor,
                                          trace_fn(state, extra))
    return state, trace_element

  if any(e is None for e in tf.nest.flatten(state)) or tf.executing_eagerly():
    state, first_trace = wrapper(state)
    trace_arrays = tf.nest.map_structure(
        lambda v: tf.TensorArray(  # pylint: disable=g-long-lambda
            v.dtype,
            size=num_steps,
            element_shape=v.shape).write(0, v),
        first_trace)
    start_idx = 1
  else:
    state_spec = tf.nest.map_structure(tf.TensorSpec.from_tensor, state)
    # We need the shapes and dtypes of the outputs of `wrapper` function to
    # create the `TensorArray`s, we can get it by pre-compiling the wrapper
    # function.
    wrapper = tf.function(autograph=False)(wrapper)
    concrete_wrapper = wrapper.get_concrete_function(state_spec)
    _, trace_dtypes = concrete_wrapper.output_dtypes
    _, trace_shapes = concrete_wrapper.output_shapes
    trace_arrays = tf.nest.map_structure(
        lambda dtype, shape: tf.TensorArray(  # pylint: disable=g-long-lambda
            dtype,
            size=num_steps,
            element_shape=shape),
        trace_dtypes,
        trace_shapes)
    wrapper = lambda state: concrete_wrapper(*tf.nest.flatten(state))
    start_idx = 0

  def body(i, state, trace_arrays):
    state, trace_element = wrapper(state)
    trace_arrays = tf.nest.map_structure(lambda a, v: a.write(i, v),
                                         trace_arrays, trace_element)
    return i + 1, state, trace_arrays

  def cond(i, *_):
    return i < num_steps

  _, state, trace_arrays = tf.while_loop(
      cond=cond,
      body=body,
      loop_vars=(start_idx, state, trace_arrays),
      parallel_iterations=parallel_iterations)

  stacked_trace = tf.nest.map_structure(lambda x: x.stack(), trace_arrays)

  static_length = tf.get_static_value(num_steps)

  def _merge_static_length(x):
    x.set_shape(tf.TensorShape(static_length).concatenate(x.shape[1:]))
    return x

  stacked_trace = tf.nest.map_structure(_merge_static_length, stacked_trace)

  return state, stacked_trace


def call_fn(fn: TransitionOperator,
            args: Union[Tuple[Any], Mapping[Text, Any], Any]) -> Any:
  """Calls a transition operator with args.

  If args is a sequence, the args are unpacked as `*args`.
  If args is a mapping, the args are unpacked as `**args`.

  Args:
    fn: A `TransitionOperator`.
    args: Arguments to `fn`

  Returns:
    ret: Return value of `fn`.
  """
  if isinstance(
      args, collections.Sequence) and not mcmc_util.is_namedtuple_like(args):
    args = args  # type: Tuple[Any]
    return fn(*args)
  elif isinstance(args, collections.Mapping):
    args = args  # type: Mapping[str, Any]
    return fn(**args)
  else:
    return fn(args)


def call_and_grads(fn: TransitionOperator,
                   args: Union[Tuple[Any], Mapping[Text, Any], Any]
                  ) -> Tuple[tf.Tensor, TensorNest, TensorNest]:
  """Calls `fn` and returns the gradients with respect to `fn`'s first output.

  Args:
    fn: A `TransitionOperator`.
    args: Arguments to `fn`

  Returns:
    ret: First output of `fn`.
    extra: Second output of `fn`.
    grads: Gradients of `ret` with respect to `args`.
  """
  with tf.GradientTape() as tape:
    tape.watch(args)
    ret, extra = call_fn(fn, args)
  grads = tape.gradient(ret, args)
  return ret, extra, grads


def maybe_broadcast_structure(from_structure: Any, to_structure: Any) -> Any:
  """Maybe broadcasts `from_structure` to `to_structure`.

  If `from_structure` is a singleton, it is tiled to match the structure of
  `to_structure`. Note that the elements in `from_structure` are not copied if
  this tiling occurs.

  Args:
    from_structure: A structure.
    to_structure: A structure.

  Returns:
    new_from_structure: Same structure as `to_structure`.
  """
  flat_from = tf.nest.flatten(from_structure)
  flat_to = tf.nest.flatten(to_structure)
  if len(flat_from) == 1:
    flat_from *= len(flat_to)
  return tf.nest.pack_sequence_as(to_structure, flat_from)


def transform_log_prob_fn(log_prob_fn: PotentialFn,
                          bijector: BijectorNest,
                          init_state: State = None) -> Any:
  """Transforms a log-prob function using a bijector.

  This takes a log-prob function and creates a new log-prob function that now
  takes takes state in the domain of the bijector, forward transforms that state
  and calls the original log-prob function. It then returns the log-probability
  that correctly accounts for this transformation.

  The wrapped function has the following signature:
  ```none
    (*args, **kwargs) ->
      transformed_space_state, [original_space_state, original_log_prob_extra]
  ```
  Note that currently it is forbidden to pass both `args` and `kwargs` to the
  wrapper.

  For convenience you can also pass the initial state (in the original space),
  and this function will return the inverse transformed state as the 2nd return
  value. You'd use this to initialize MCMC operators that operate in the
  transformed space.

  Args:
    log_prob_fn: Log prob fn.
    bijector: Bijector(s), must be of the same structure as the `log_prob_fn`
      inputs.
    init_state: Initial state, in the original space.

  Returns:
    transformed_log_prob_fn: Transformed log prob fn.
    transformed_init_state: If `init_state` is provided. Initial state in the
      transformed space.
  """

  def wrapper(*args, **kwargs):
    """Transformed wrapper."""
    bijector_ = bijector

    if args and kwargs:
      raise ValueError('It is forbidden to pass both `args` and `kwargs` to '
                       'this wrapper.')
    if kwargs:
      args = kwargs
    # Use bijector_ to recover the structure of args that has been lossily
    # transmitted via *args and **kwargs.
    args = tf.nest.pack_sequence_as(bijector_, tf.nest.flatten(args))

    args = tf.nest.map_structure(lambda x: 0. + x, args)

    original_space_args = tf.nest.map_structure(lambda b, x: b.forward(x),
                                                bijector_, args)
    original_space_log_prob, extra = call_fn(log_prob_fn, original_space_args)
    event_ndims = tf.nest.map_structure(
        lambda x: tf.rank(x) - tf.rank(original_space_log_prob), args)

    return original_space_log_prob + sum(
        tf.nest.flatten(
            tf.nest.map_structure(
                lambda b, x, e: b.forward_log_det_jacobian(x, event_ndims=e),
                bijector_, args, event_ndims))), [original_space_args, extra]

  if init_state is None:
    return wrapper
  else:
    return wrapper, tf.nest.map_structure(lambda b, s: b.inverse(s), bijector,
                                          init_state)


IntegratorStepState = collections.namedtuple('IntegratorStepState',
                                             'state, state_grads, momentum')
IntegratorStepExtras = collections.namedtuple(
    'IntegratorStepExtras', 'target_log_prob, state_extra, '
    'kinetic_energy, kinetic_energy_extra')
IntegratorStep = Callable[[IntegratorStepState],
                          Tuple[IntegratorStepState, IntegratorStepExtras]]


def symmetric_spliting_integrator_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
    coefficients: Sequence[FloatTensor],
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Symmetric symplectic integrator `TransitionOperator`.

  This implementation is based on Hamiltonian splitting, with the splits
  weighted by coefficients. We assume a symmetric integrator, so only the first
  `N / 2 + 1` coefficients need be specified. We always update the momentum
  first. See [1] for an overview of the method.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.
    coefficients: First N / 2 + 1 coefficients.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Sergio Blanes, Fernando Casas, J.M. Sanz-Serna. Numerical integrators for
       the Hybrid Monte Carlo method. SIAM J. Sci. Comput., 36(4), 2014.
       https://arxiv.org/pdf/1405.3153.pdf
  """
  if len(coefficients) < 2:
    raise ValueError('Too few coefficients. Need at least 2.')
  coefficients = list(coefficients) + list(reversed(coefficients))[1:]
  state = integrator_step_state.state
  state_grads = integrator_step_state.state_grads
  momentum = integrator_step_state.momentum
  step_size = maybe_broadcast_structure(step_size, state)

  state = tf.nest.map_structure(tf.convert_to_tensor, state)
  momentum = tf.nest.map_structure(tf.convert_to_tensor, momentum)
  state = tf.nest.map_structure(tf.convert_to_tensor, state)

  if state_grads is None:
    _, _, state_grads = call_and_grads(target_log_prob_fn, state)
  else:
    state_grads = tf.nest.map_structure(tf.convert_to_tensor, state_grads)

  for i, c in enumerate(coefficients):
    # pylint: disable=cell-var-from-loop
    if i % 2 == 0:
      momentum = tf.nest.map_structure(lambda m, sg, s: m + c * sg * s,
                                       momentum, state_grads, step_size)

      kinetic_energy, kinetic_energy_extra, momentum_grads = call_and_grads(
          kinetic_energy_fn, momentum)
    else:
      state = tf.nest.map_structure(lambda x, mg, s: x + c * mg * s, state,
                                    momentum_grads, step_size)

      target_log_prob, state_extra, state_grads = call_and_grads(
          target_log_prob_fn, state)

  return (IntegratorStepState(state, state_grads, momentum),
          IntegratorStepExtras(target_log_prob, state_extra, kinetic_energy,
                               kinetic_energy_extra))


def leapfrog_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Leapfrog integrator `TransitionOperator`.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.
  """
  coefficients = [0.5, 1.]
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def ruth4_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Ruth 4th order integrator `TransitionOperator`.

  See [1] for details.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Ruth, Ronald D. (August 1983). "A Canonical Integration Technique".
       Nuclear Science, IEEE Trans. on. NS-30 (4): 2669-2671
  """
  c = 2**(1. / 3)
  coefficients = (1. / (2 - c)) * np.array([0.5, 1., 0.5 - 0.5 * c, -c])
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def blanes_3_stage_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Blanes 4th order integrator `TransitionOperator`.

  See [1] for details.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Sergio Blanes, Fernando Casas, J.M. Sanz-Serna. Numerical integrators for
       the Hybrid Monte Carlo method. SIAM J. Sci. Comput., 36(4), 2014.
       https://arxiv.org/pdf/1405.3153.pdf
  """
  a1 = 0.11888010966
  b1 = 0.29619504261
  coefficients = [a1, b1, 0.5 - a1, 1. - 2. * b1]
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def blanes_4_stage_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Blanes 6th order integrator `TransitionOperator`.

  See [1] for details.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Sergio Blanes, Fernando Casas, J.M. Sanz-Serna. Numerical integrators for
       the Hybrid Monte Carlo method. SIAM J. Sci. Comput., 36(4), 2014.
       https://arxiv.org/pdf/1405.3153.pdf
  """
  a1 = 0.071353913
  a2 = 0.268548791
  b1 = 0.191667800
  coefficients = [a1, b1, a2, 0.5 - b1, 1. - 2. * (a1 + a2)]
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def metropolis_hastings_step(current_state: State,
                             proposed_state: State,
                             energy_change: FloatTensor,
                             log_uniform: FloatTensor = None,
                             seed=None) -> Tuple[State, tf.Tensor, tf.Tensor]:
  """Metropolis-Hastings step.

  This probabilistically chooses between `current_state` and `proposed_state`
  based on the `energy_change` so as to preserve detailed balance.

  Energy change is the negative of `log_accept_ratio`.

  Args:
    current_state: Current state.
    proposed_state: Proposed state.
    energy_change: E(proposed_state) - E(previous_state).
    log_uniform: Optional logarithm of a uniformly distributed random sample in
      [0, 1]. It is used to accept/reject the current and proposed state.
    seed: For reproducibility.

  Returns:
    new_state: The chosen state.
    is_accepted: Whether the proposed state was accepted.
    log_uniform: The random number that was used to select between the two
      states.
  """
  flat_current = tf.nest.flatten(current_state)
  flat_proposed = nest.flatten_up_to(current_state, proposed_state)
  # Impute the None's in the current state.
  flat_current = [
      p if c is None else c for p, c in zip(flat_proposed, flat_current)
  ]
  current_state = tf.nest.pack_sequence_as(current_state, flat_current)

  current_state = tf.nest.map_structure(tf.convert_to_tensor, current_state)
  proposed_state = tf.nest.map_structure(tf.convert_to_tensor, proposed_state)
  energy_change = tf.convert_to_tensor(value=energy_change)

  log_accept_ratio = -energy_change

  if log_uniform is None:
    log_uniform = tf.math.log(
        tf.random.uniform(
            shape=tf.shape(input=log_accept_ratio),
            dtype=log_accept_ratio.dtype.base_dtype,
            seed=seed))
  is_accepted = log_uniform < log_accept_ratio

  next_state = _choose(
      is_accepted, proposed_state, current_state, name='choose_next_state')
  return next_state, is_accepted, log_uniform


MomentumSampleFn = Callable[[], State]


def gaussian_momentum_sample(state_spec: TensorSpecNest = None,
                             state: State = None) -> State:
  """Generates a sample from a Gaussian (Normal) momentum distribution.

  One of `state` or `state_spec` need to be specified to obtain the correct
  structure.

  Args:
    state_spec: A nest of `TensorSpec`s describing the output shape and dtype.
    state: A nest of `Tensor`s with the shape and dtype being the same as the
      output.

  Returns:
    sample: A nest of `Tensor`s with the same structure, shape and dtypes as one
      of the two inputs, distributed with Normal distribution.
  """
  if state_spec is None:
    if state is None:
      raise ValueError(
          'If `state_spec` is `None`, then `state` must be specified.')
    shapes = tf.nest.map_structure(tf.shape, state)
    dtypes = tf.nest.map_structure(lambda t: t.dtype, state)
  else:
    shapes = tf.nest.map_structure(lambda spec: spec.shape, state_spec)
    dtypes = tf.nest.map_structure(lambda spec: spec.dtype, state_spec)
  return tf.nest.map_structure(
      lambda shape, dtype: tf.random.normal(shape, dtype=dtype), shapes, dtypes)


def make_gaussian_kinetic_energy_fn(
    chain_ndims: IntTensor) -> Callable[..., Tuple[tf.Tensor, TensorNest]]:
  """Returns a function that computes the kinetic energy of a state.

  Args:
    chain_ndims: How many leading dimensions correspond to independent
      particles.

  Returns:
    kinetic_energy_fn: A callable that takes in the expanded state (see
      `call_fn`) and returns the kinetic energy + dummy auxiliary output.
  """

  def kinetic_energy_fn(*args, **kwargs):
    def one_component(x):
      return tf.reduce_sum(tf.square(x), axis=tf.range(chain_ndims, tf.rank(x)))

    return (
        tf.add_n([one_component(x) for x in tf.nest.flatten([args, kwargs])]) /
        2.), ()

  return kinetic_energy_fn


class HamiltonianMonteCarloState(
    collections.namedtuple('HamiltonianMonteCarloState',
                           'state, state_grads, target_log_prob, state_extra')):
  """State of `hamiltonian_monte_carlo`."""
  __slots__ = ()

  def __new__(cls,
              state,
              state_grads=None,
              target_log_prob=None,
              state_extra=None):
    return super(HamiltonianMonteCarloState,
                 cls).__new__(cls, state, state_grads, target_log_prob,
                              state_extra)


# state_extra is not a true state, but here for convenience.
HamiltonianMonteCarloExtra = collections.namedtuple(
    'HamiltonianMonteCarloExtra',
    'is_accepted, log_accept_ratio, proposed_hmc_state, '
    'integrator_state, integrator_extra, initial_momentum')


def hamiltonian_monte_carlo_init(state: TensorNest,
                                 target_log_prob_fn: PotentialFn
                                ) -> HamiltonianMonteCarloState:
  """Initializes the `HamiltonianMonteCarloState`.

  Args:
    state: State of the chain.
    target_log_prob_fn: Target log prob fn.

  Returns:
    hmc_state: State of the `hamiltonian_monte_carlo` `TransitionOperator`.
  """
  target_log_prob, state_extra, state_grads = call_and_grads(
      target_log_prob_fn, tf.nest.map_structure(tf.convert_to_tensor, state))
  return HamiltonianMonteCarloState(state, state_grads, target_log_prob,
                                    state_extra)


def hamiltonian_monte_carlo(
    hmc_state: HamiltonianMonteCarloState,
    target_log_prob_fn: PotentialFn,
    step_size: Any = None,
    num_integrator_steps: IntTensor = None,
    momentum: State = None,
    kinetic_energy_fn: PotentialFn = None,
    momentum_sample_fn: MomentumSampleFn = None,
    integrator_trace_fn: Callable[[IntegratorStepState, IntegratorStepExtras],
                                  TensorNest] = lambda *args: (),
    log_uniform: FloatTensor = None,
    integrator_fn=None,
    seed=None,
) -> Tuple[HamiltonianMonteCarloState, HamiltonianMonteCarloExtra]:
  """Hamiltonian Monte Carlo `TransitionOperator`.

  #### Example

  ```python
  step_size = 0.2
  num_steps = 2000
  num_integrator_steps = 10
  state = tf.ones([16, 2])

  base_mean = [1., 0]
  base_cov = [[1, 0.5], [0.5, 1]]

  bijector = tfb.Softplus()
  base_dist = tfd.MultivariateNormalFullCovariance(
      loc=base_mean, covariance_matrix=base_cov)
  target_dist = bijector(base_dist)

  def orig_target_log_prob_fn(x):
    return target_dist.log_prob(x), ()

  target_log_prob_fn, state = fun_mcmc.transform_log_prob_fn(
      orig_target_log_prob_fn, bijector, state)

  kernel = tf.function(lambda state: fun_mcmc.hamiltonian_monte_carlo(
      state,
      step_size=step_size,
      num_integrator_steps=num_integrator_steps,
      target_log_prob_fn=target_log_prob_fn,
      seed=tfp_test_util.test_seed()))

  _, chain = fun_mcmc.trace(
      state=fun_mcmc.hamiltonian_monte_carlo_init(state, target_log_prob_fn),
      fn=kernel,
      num_steps=num_steps,
      trace_fn=lambda state, extra: state.state_extra[0])
  ```

  Args:
    hmc_state: HamiltonianMonteCarloState.
    target_log_prob_fn: Target log prob fn.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state. Optional if `integrator_fn` is specified.
    num_integrator_steps: Number of integrator steps to take. Optional if
      `integrator_fn` is specified.
    momentum: Initial momentum, by default sampled from a standard gaussian.
    kinetic_energy_fn: Kinetic energy function.
    momentum_sample_fn: Sampler for the momentum.
    integrator_trace_fn: Trace function for the integrator.
    log_uniform: Optional logarithm of a uniformly distributed random sample in
      [0, 1], used for the MH accept/reject step.
    integrator_fn: Integrator to use for the HMC dynamics. Uses a
      `hamiltonian_integrator` with `leapfrog_step` by default.
    seed: For reproducibility.

  Returns:
    hmc_state: HamiltonianMonteCarloState
    hmc_extra: HamiltonianMonteCarloExtra
  """
  if any(e is None for e in tf.nest.flatten(hmc_state)):
    hmc_state = hamiltonian_monte_carlo_init(hmc_state.state,
                                             target_log_prob_fn)
  state = hmc_state.state
  state_grads = hmc_state.state_grads
  target_log_prob = hmc_state.target_log_prob
  state_extra = hmc_state.state_extra

  if kinetic_energy_fn is None:
    kinetic_energy_fn = make_gaussian_kinetic_energy_fn(
        target_log_prob.shape.ndims if target_log_prob.shape else tf
        .rank(target_log_prob))

  if momentum_sample_fn is None:
    momentum_sample_fn = lambda: gaussian_momentum_sample(state=state)

  if integrator_fn is None:
    integrator_fn = lambda state: hamiltonian_integrator(  # pylint: disable=g-long-lambda
        state,
        num_steps=num_integrator_steps,
        integrator_step_fn=lambda state: leapfrog_step(  # pylint: disable=g-long-lambda
            state,
            step_size=step_size,
            target_log_prob_fn=target_log_prob_fn,
            kinetic_energy_fn=kinetic_energy_fn),
        kinetic_energy_fn=kinetic_energy_fn,
        integrator_trace_fn=integrator_trace_fn)

  if momentum is None:
    momentum = momentum_sample_fn()

  integrator_state = IntegratorState(
      target_log_prob=target_log_prob,
      momentum=momentum,
      state=state,
      state_grads=state_grads,
      state_extra=state_extra,
  )

  integrator_state, integrator_extra = integrator_fn(integrator_state)

  proposed_state = HamiltonianMonteCarloState(
      state=integrator_state.state,
      state_grads=integrator_state.state_grads,
      target_log_prob=integrator_state.target_log_prob,
      state_extra=integrator_state.state_extra)

  hmc_state, is_accepted, _ = metropolis_hastings_step(
      hmc_state,
      proposed_state,
      integrator_extra.energy_change,
      log_uniform=log_uniform,
      seed=seed)

  hmc_state = hmc_state  # type: HamiltonianMonteCarloState
  return hmc_state, HamiltonianMonteCarloExtra(
      is_accepted=is_accepted,
      proposed_hmc_state=proposed_state,
      log_accept_ratio=-integrator_extra.energy_change,
      integrator_state=integrator_state,
      integrator_extra=integrator_extra,
      initial_momentum=momentum)


IntegratorState = collections.namedtuple(
    'IntegratorState',
    'state, state_extra, state_grads, target_log_prob, momentum')
IntegratorExtras = collections.namedtuple(
    'IntegratorExtras',
    'kinetic_energy, kinetic_energy_extra, energy_change, integrator_trace')


def hamiltonian_integrator(
    int_state: IntegratorState,
    num_steps: IntTensor,
    integrator_step_fn: IntegratorStep,
    kinetic_energy_fn: PotentialFn,
    integrator_trace_fn: Callable[[IntegratorStepState, IntegratorStepExtras],
                                  TensorNest] = lambda *args: (),
) -> Tuple[IntegratorState, IntegratorExtras]:
  """Intergrates a discretized set of Hamiltonian equations.

  This function will use the passed `integrator_step_fn` to evolve the system
  for `num_steps`. The `integrator_step_fn` is assumed to be reversible.

  Args:
    int_state: Current `IntegratorState`.
    num_steps: Integer scalar or N-D `Tensor`. Number of steps to take. If this
      is not a scalar, then each corresponding independent system will be
      evaluated for that number of steps, followed by copying the final state to
      avoid creating a ragged Tensor. Keep this in mind when interpreting the
      `integrator_trace` in the auxiliary output.
    integrator_step_fn: Instance of `IntegratorStep`.
    kinetic_energy_fn: Function to compute the kinetic energy from momentums.
    integrator_trace_fn: Trace function for the integrator.

  Returns:
    integrator_state: `IntegratorState`
    integrator_exras: `IntegratorExtras`
  """
  target_log_prob = int_state.target_log_prob
  momentum = int_state.momentum
  state = int_state.state
  state_grads = int_state.state_grads
  state_extra = int_state.state_extra

  num_steps = tf.convert_to_tensor(num_steps)
  is_ragged = num_steps.shape.ndims > 0

  kinetic_energy, kinetic_energy_extra = call_fn(kinetic_energy_fn, momentum)
  current_energy = -target_log_prob + kinetic_energy

  if is_ragged:
    step = 0
    max_num_steps = tf.reduce_max(num_steps)
  else:
    step = []
    max_num_steps = num_steps

  # We need to carry around the integrator state extras so we can properly do
  # the ragged computation.
  # TODO(siege): In principle we can condition this on `is_ragged`, but doesn't
  # seem worthwhile at the time.
  integrator_wrapper_state = (step,
                              IntegratorStepState(state, state_grads, momentum),
                              IntegratorStepExtras(target_log_prob, state_extra,
                                                   kinetic_energy,
                                                   kinetic_energy_extra))

  def integrator_wrapper(step, integrator_step_state, integrator_step_extra):
    """Integrator wrapper that tracks extra state."""
    old_integrator_step_state = integrator_step_state
    old_integrator_step_extra = integrator_step_extra
    integrator_step_state, integrator_step_extra = integrator_step_fn(
        integrator_step_state)

    if is_ragged:
      integrator_step_state = _choose(step < num_steps, integrator_step_state,
                                      old_integrator_step_state)
      integrator_step_extra = _choose(step < num_steps, integrator_step_extra,
                                      old_integrator_step_extra)
      step = step + 1

    return (step, integrator_step_state, integrator_step_extra), []

  def integrator_trace_wrapper_fn(args, _):
    return integrator_trace_fn(args[1], args[2])

  [_, integrator_step_state, integrator_step_extra], integrator_trace = trace(
      integrator_wrapper_state,
      integrator_wrapper,
      max_num_steps,
      trace_fn=integrator_trace_wrapper_fn)

  proposed_energy = (-integrator_step_extra.target_log_prob +
                     integrator_step_extra.kinetic_energy)

  energy_change = proposed_energy - current_energy

  state = IntegratorState(
      state=integrator_step_state.state,
      state_extra=integrator_step_extra.state_extra,
      state_grads=integrator_step_state.state_grads,
      target_log_prob=integrator_step_extra.target_log_prob,
      momentum=integrator_step_state.momentum)

  extra = IntegratorExtras(
      kinetic_energy=integrator_step_extra.kinetic_energy,
      kinetic_energy_extra=integrator_step_extra.kinetic_energy_extra,
      energy_change=energy_change,
      integrator_trace=integrator_trace)

  return state, extra


def sign_adaptation(control: FloatNest,
                    output: FloatTensor,
                    set_point: FloatTensor,
                    adaptation_rate: FloatTensor = 0.01) -> FloatNest:
  """A function to do simple sign-based control of a variable.

  ```
  control = control * (1. + adaptation_rate) ** sign(output - set_point)
  ```

  Args:
    control: The control variable.
    output: The output variable.
    set_point: The set point for `output`. This function will adjust `control`
      so that `output` matches `set_point`.
    adaptation_rate: Adaptation rate.

  Returns:
    control: New control.
  """

  def _get_new_control(control, output, set_point):
    new_control = _choose(output > set_point, control * (1. + adaptation_rate),
                          control / (1. + adaptation_rate))
    return new_control

  output = maybe_broadcast_structure(output, control)
  set_point = maybe_broadcast_structure(set_point, control)

  return tf.nest.map_structure(_get_new_control, control, output, set_point)


def transition_kernel_wrapper(current_state: FloatNest,
                              kernel_results: Optional[Any],
                              kernel: tfp.mcmc.TransitionKernel
                             ) -> Tuple[FloatNest, Any]:
  """Wraps a `tfp.mcmc.TransitionKernel` as a `TransitionOperator`.

  Args:
    current_state: Current state passed to the transition kernel.
    kernel_results: Kernel results passed to the transition kernel. Can be
      `None`.
    kernel: The transition kernel.

  Returns:
    state: A tuple of:
      current_state: Current state returned by the transition kernel.
      kernel_results: Kernel results returned by the transition kernel.
    extra: An empty tuple.
  """
  flat_current_state = tf.nest.flatten(current_state)
  if kernel_results is None:
    kernel_results = kernel.bootstrap_results(flat_current_state)
  flat_current_state, kernel_results = kernel.one_step(flat_current_state,
                                                       kernel_results)
  return (tf.nest.pack_sequence_as(current_state,
                                   flat_current_state), kernel_results), ()


def _choose(is_accepted, accepted, rejected, name='choose'):
  """Helper which expand_dims `is_accepted` then applies tf.where."""

  def _choose_base_case(is_accepted, accepted, rejected, name):
    """Choose base case for one tensor."""

    def _expand_is_accepted_like(x):
      """Helper to expand `is_accepted` like the shape of some input arg."""
      with tf.name_scope('expand_is_accepted_like'):
        expand_shape = tf.concat([
            tf.shape(input=is_accepted),
            tf.ones([tf.rank(x) - tf.rank(is_accepted)], dtype=tf.int32),
        ],
                                 axis=0)
        return tf.reshape(is_accepted, expand_shape)

    with tf.name_scope(name):
      if accepted is rejected:
        return accepted
      accepted = tf.convert_to_tensor(value=accepted, name='accepted')
      rejected = tf.convert_to_tensor(value=rejected, name='rejected')
      return tf.where(_expand_is_accepted_like(accepted), accepted, rejected)

  return tf.nest.map_structure(
      lambda a, r: _choose_base_case(is_accepted, a, r, name=name), accepted,
      rejected)

AdamState = collections.namedtuple('AdamState', 'state, m, v, t')
AdamExtra = collections.namedtuple('AdamExtra', 'loss, loss_extra')


def adam_init(state: FloatNest) -> AdamState:
  state = tf.nest.map_structure(tf.convert_to_tensor, state)
  return AdamState(
      state=state,
      m=tf.nest.map_structure(tf.zeros_like, state),
      v=tf.nest.map_structure(tf.zeros_like, state),
      t=tf.constant(0, dtype=tf.int32))


def adam_step(adam_state: AdamState,
              loss_fn: PotentialFn,
              learning_rate: FloatNest,
              beta_1: FloatNest = 0.9,
              beta_2: FloatNest = 0.999,
              epsilon: FloatNest = 1e-8) -> Tuple[AdamState, AdamExtra]:
  """Perform one step of the Adam optimization method.

  Args:
    adam_state: Current `AdamState`.
    loss_fn: A function whose output will be minimized.
    learning_rate: Learning rate, broadcastable with the state.
    beta_1: Adaptation rate for the first order gradient statistics,
      broadcastable with the state.
    beta_2: Adaptation rate for the second order gradient statistics,
      broadcastable with the state.
    epsilon: Epsilon to stabilize the algorithm, broadcastable with the state.

  Note that the `epsilon` is actually the `epsilon_hat` from introduction to
  Section 2 in [1].

  Returns:
    adam_state: `AdamState`
    adam_extra: `AdamExtra`


  #### References:

  [1]: Kingma, D. P., & Ba, J. L. (2015). Adam: a Method for Stochastic
       Optimization. International Conference on Learning Representations
       2015, 1-15.
  """
  if any(e is None for e in tf.nest.flatten(adam_state)):
    adam_state = adam_init(adam_state.state)
  state = adam_state.state
  m = adam_state.m
  v = adam_state.v
  learning_rate = maybe_broadcast_structure(learning_rate, state)
  beta_1 = maybe_broadcast_structure(beta_1, state)
  beta_2 = maybe_broadcast_structure(beta_2, state)
  epsilon = maybe_broadcast_structure(epsilon, state)
  t = tf.cast(adam_state.t + 1, tf.float32)

  def _one_part(state, g, m, v, learning_rate, beta_1, beta_2, epsilon):
    lr_t = learning_rate * (
        tf.math.sqrt(1. - tf.math.pow(beta_2, t)) /
        (1. - tf.math.pow(beta_1, t)))

    m_t = beta_1 * m + (1. - beta_1) * g
    v_t = beta_2 * v + (1. - beta_2) * tf.square(g)
    state = state - lr_t * m_t / (tf.math.sqrt(v_t) + epsilon)
    return state, m_t, v_t

  loss, loss_extra, grads = call_and_grads(loss_fn, state)

  state_m_v = tf.nest.map_structure(_one_part, state, grads, m, v,
                                    learning_rate, beta_1, beta_2, epsilon)

  adam_state = AdamState(
      state=nest.map_structure_up_to(state, lambda x: x[0], state_m_v),
      m=nest.map_structure_up_to(state, lambda x: x[1], state_m_v),
      v=nest.map_structure_up_to(state, lambda x: x[2], state_m_v),
      t=adam_state.t + 1)

  return adam_state, AdamExtra(loss_extra=loss_extra, loss=loss)
