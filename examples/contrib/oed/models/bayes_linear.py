import warnings
from collections import OrderedDict
from functools import partial
import torch
from torch.nn.functional import softplus
import numpy as np

import pyro
import pyro.distributions as dist


def known_covariance_linear_model(coef_mean, coef_sd, observation_sd,
                                  coef_label="w", observation_label="y"):
    model = partial(bayesian_linear_model, 
                    w_means={coef_label: coef_mean},
                    w_sqrtlambdas={coef_label: 1./(observation_sd*coef_sd)},
                    obs_sd=observation_sd,
                    response_label=observation_label)
    # For testing, add these
    model.obs_sd = observation_sd
    model.w_sds = {coef_label: coef_sd}
    return model


def normal_guide(observation_sd, coef_shape, coef_label="w"):
    return partial(normal_inv_gamma_family_guide, 
                   obs_sd=observation_sd,
                   w_sizes={coef_label: coef_shape})


def group_linear_model(coef1_mean, coef1_sd, coef2_mean, coef2_sd, observation_sd,
                       coef1_label="w1", coef2_label="w2", observation_label="y"):
    model = partial(
        bayesian_linear_model, w_means={coef1_label: coef1_mean, coef2_label: coef2_mean},
        w_sqrtlambdas={coef1_label: 1./(observation_sd*coef1_sd), coef2_label: 1./(observation_sd*coef2_sd)},
        obs_sd=observation_sd)
    model.obs_sd = observation_sd
    model.w_sds = {coef1_label: coef1_sd, coef2_label: coef2_sd}
    return model


def group_normal_guide(observation_sd, coef1_shape, coef2_shape,
                       coef1_label="w1", coef2_label="w2"):
    return partial(
        normal_inv_gamma_family_guide, w_sizes={coef1_label: coef1_shape, coef2_label: coef2_shape},
        obs_sd=observation_sd)


def zero_mean_unit_obs_sd_lm(coef_sd):
    model = known_covariance_linear_model(torch.tensor(0.), coef_sd, torch.tensor(1.))
    guide = normal_guide(torch.tensor(1.), coef_sd.shape)
    return model, guide


def normal_inverse_gamma_linear_model(coef_mean, coef_sqrtlambda, alpha,
                                      beta, coef_label="w",
                                      observation_label="y"):
    return partial(bayesian_linear_model, 
                   w_means={coef_label: coef_mean},
                   w_sqrtlambdas={coef_label: coef_sqrtlambda},
                   alpha_0=alpha, beta_0=beta,
                   response_label=observation_label)


def normal_inverse_gamma_guide(coef_shape, coef_label="w"):
    return partial(normal_inv_gamma_family_guide, obs_sd=None, w_sizes={coef_label: coef_shape})


def logistic_regression_model(coef_mean, coef_sd, coef_label="w", observation_label="y"):
    return partial(bayesian_linear_model,
                   w_means={coef_label: coef_mean},
                   w_sqrtlambdas={coef_label: 1./coef_sd},
                   obs_sd=torch.tensor(1.),
                   response="bernoulli",
                   response_label=observation_label)


def lmer_model(fixed_effects_sd, n_groups, random_effects_alpha, random_effects_beta,
               fixed_effects_label="w", random_effects_label="u", observation_label="y",
               response="normal"):
    return partial(bayesian_linear_model,
                   w_means={fixed_effects_label: torch.tensor(0.)},
                   w_sqrtlambdas={fixed_effects_label: 1./fixed_effects_sd},
                   obs_sd=torch.tensor(1.),
                   re_group_sizes={random_effects_label: n_groups},
                   re_alphas={random_effects_label: random_effects_alpha},
                   re_betas={random_effects_label: random_effects_beta},
                   response=response,
                   response_label=observation_label)


def bayesian_linear_model(design, w_means={}, w_sqrtlambdas={}, re_group_sizes={},
                          re_alphas={}, re_betas={}, obs_sd=None,
                          alpha_0=None, beta_0=None, response="normal",
                          response_label="y"):
    """
    A pyro model for Bayesian linear regression.

    If :param:`response` is `"normal"` this corresponds to a linear regression
    model

        :math:`Y = Xw + \\epsilon`

    with `\\epsilon`` i.i.d. zero-mean Gaussian. The observation standard deviation
    (:param:`obs_sd`) may be known or unknown. If unknown, it is assumed to follow an
    inverse Gamma distribution with parameters :param:`alpha_0` and :param:`beta_0`.

    If the response type is `"bernoulli"` we instead have :math:`Y \\sim Bernoulli(p)`
    with

        :math:`logit(p) = Xw`

    Given parameter groups in :param:`w_means` and :param:`w_sqrtlambda`, the fixed effects
    regression coefficient is taken to be Gaussian with mean `w_mean` and standard deviation
    given by

        :math:`\\sigma / \\sqrt{\\lambda}`

    corresponding to the normal inverse Gamma family.

    The random effects coefficient is constructed as follows. For each random effect
    group, standard deviations for that group are sampled from a normal inverse Gamma
    distribution. For each group, a random effect coefficient is then sampled from a zero 
    mean Gaussian with those standard deviations.

    :param torch.Tensor design: a tensor with last two dimensions `n` and `p`
            corresponding to observations and features respectively.
    :param OrderedDict w_means: map from variable names to tensors of fixed effect means.
    :param OrderedDict w_sqrtlambdas: map from variable names to tensors of square root
        :math:`\\lambda` values for fixed effects.
    :param OrderedDict re_group_sizes: map from variable names to int representing the
        group size
    :param OrderedDict re_alphas: map from variable names to `torch.Tensor`, the tensor
        consists of Gamma dist :math:`\\alpha` values
    :param OrderedDict re_betas: map from variable names to `torch.Tensor`, the tensor
        consists of Gamma dist :math:`\\beta` values
    :param torch.Tensor obs_sd: the observation standard deviation (if assumed known).
        This is still relevant in the case of Bernoulli observations when coefficeints
        are sampled using `w_sqrtlambdas`.
    :param torch.Tensor alpha_0: Gamma :math:`\\alpha` parameter for unknown observation
        covariance.
    :param torch.Tensor beta_0: Gamma :math:`\\beta` parameter for unknown observation
        covariance.
    :param str response: Emission distribution. May be `"normal"` or `"bernoulli"`.
    :param str response_label: Variable label for response.
    """
    # design is size batch x n x p
    # tau is size batch
    tau_shape = design.shape[:-2]
    if obs_sd is None:
        # First, sample tau (observation precision)
        tau_prior = dist.Gamma(alpha_0.expand(tau_shape),
                               beta_0.expand(tau_shape))
        tau = pyro.sample("tau", tau_prior)
        obs_sd = 1./torch.sqrt(tau)

    elif alpha_0 is not None or beta_0 is not None:
        warnings.warn("Values of `alpha_0` and `beta_0` unused becased"
                      "`obs_sd` was specified already.")

    # response will be shape batch x n
    obs_sd = obs_sd.expand(tau_shape).unsqueeze(-1)

    # Build the regression coefficient
    w = []
    # Allow different names for different coefficient groups
    # Process fixed effects
    for name, w_sqrtlambda in w_sqrtlambdas.items():
        w_mean = w_means[name]
        # Place a normal prior on the regression coefficient
        w_prior = dist.Normal(w_mean, obs_sd / w_sqrtlambda).independent(1)
        w.append(pyro.sample(name, w_prior).unsqueeze(-1))
    # Process random effects
    for name, group_size in re_group_sizes.items():
        # Sample `G` once for this group
        alpha, beta = re_alphas[name], re_betas[name]
        group_p = alpha.shape[-1]
        G_prior = dist.Gamma(alpha.expand(tau_shape + (group_p,)),
                             beta.expand(tau_shape + (group_p,)))
        G = 1./torch.sqrt(pyro.sample("G_" + name, G_prior))
        # Repeat `G` for each group
        repeat_shape = tuple(1 for _ in tau_shape) + (group_size,)
        u_prior = dist.Normal(torch.tensor(0.), G.repeat(repeat_shape)).independent(1)
        w.append(pyro.sample(name, u_prior).unsqueeze(-1))
    # Regression coefficient `w` is batch x p x 1
    w = torch.cat(w, dim=-2)

    # Run the regressor forward conditioned on inputs
    prediction_mean = torch.matmul(design, w).squeeze(-1)
    if response == "normal":
        # y is an n-vector: hence use .independent(1)
        return pyro.sample(response_label, dist.Normal(prediction_mean, obs_sd).independent(1))
    elif response == "bernoulli":
        return pyro.sample(response_label, dist.Bernoulli(logits=prediction_mean).independent(1))
    else:
        raise ValueError("Unknown response distribution: '{}'".format(response))

    return model


def normal_inv_gamma_family_guide(design, obs_sd, w_sizes):
    """Normal inverse Gamma family guide.

    If `obs_sd` is known, this is a multivariate Normal family with separate
    parameters for each batch. `w` is sampled from a Gaussian with mean `mw_param` and
    covariance matrix derived from  `obs_sd * lambda_param` and the two parameters `mw_param` and `lambda_param`
    are learned.

    If `obs_sd=None`, this is a four-parameter family. The observation precision
    `tau` is sampled from a Gamma distribution with parameters `alpha`, `beta`
    (separate for each batch). We let `obs_sd = 1./torch.sqrt(tau)` and then
    proceed as above.

    :param torch.Tensor design: a tensor with last two dimensions `n` and `p`
        corresponding to observations and features respectively.
    :param torch.Tensor obs_sd: observation standard deviation, or `None` to use
        inverse Gamma
    :param OrderedDict w_sizes: map from variable names to torch.Size
    """
    # design is size batch x n x p
    # tau is size batch
    tau_shape = design.shape[:-2]
    if obs_sd is None:
        # First, sample tau (observation precision)
        alpha = softplus(pyro.param("invsoftplus_alpha", 3.*torch.ones(tau_shape)))
        beta = softplus(pyro.param("invsoftplus_beta", 3.*torch.ones(tau_shape)))
        # Global variable
        tau_prior = dist.Gamma(alpha, beta)
        tau = pyro.sample("tau", tau_prior)
        obs_sd = 1./torch.sqrt(tau)

    # response will be shape batch x n
    obs_sd = obs_sd.expand(tau_shape).unsqueeze(-1)

    for name, size in w_sizes.items():
        w_shape = tau_shape + size
        # Set up mu and lambda
        mw_param = pyro.param("{}_guide_mean".format(name), torch.zeros(w_shape))
        sqrtlambda_param = softplus(pyro.param("{}_guide_sqrtlambda".format(name),
                                               3.*torch.ones(w_shape)))
        # guide distributions for w
        w_dist = dist.Normal(mw_param, obs_sd / sqrtlambda_param).independent(1)
        pyro.sample(name, w_dist)


def group_assignment_matrix(design):
    """Converts a one-dimensional tensor listing group sizes into a
    two-dimensional binary tensor of indicator variables.

    :return: A :math:`n \times p` binary matrix where :math:`p` is
        the length of `design` and :math:`n` is its sum. There are
        :math:`n_i` ones in the :math:`i`th column.
    :rtype: torch.tensor

    """
    n, p = int(torch.sum(design)), int(design.shape[0])
    X = torch.zeros(n, p)
    t = 0
    for col, i in enumerate(design):
        i = int(i)
        if i > 0:
            X[t:t+i, col] = 1.
        t += i
    if t < n:
        X[t:, -1] = 1.
    return X


def analytic_posterior_cov(prior_cov, x, obs_sd):
    """
    Given a prior covariance matrix and a design matrix `x`,
    returns the covariance of the posterior under a Bayesian
    linear regression model with design `x` and observation
    noise `obs_sd`.
    """
    # Use some kernel trick magic
    p = prior_cov.shape[-1]
    SigmaXX = prior_cov.mm(x.t().mm(x))
    posterior_cov = prior_cov - torch.inverse(
        SigmaXX + (obs_sd**2)*torch.eye(p)).mm(SigmaXX.mm(prior_cov))
    return posterior_cov
