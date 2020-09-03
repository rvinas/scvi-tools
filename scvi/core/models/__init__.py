import torch
import pickle
import os
import logging
import pandas as pd
import numpy as np
import inspect

from anndata import AnnData
from functools import partial
from scvi.core._distributions import NegativeBinomial, ZeroInflatedNegativeBinomial
from scvi.models._differential import DifferentialComputation
from scvi.models._utils import (
    scrna_raw_counts_properties,
    _get_var_names_from_setup_anndata,
    _get_batch_code_from_category,
)
from scvi.dataset._utils import (
    _check_anndata_setup_equivalence,
    _check_nonnegative_integers,
)
from scvi import _CONSTANTS
from typing import Optional, Union, List, Dict, Sequence
from scvi._compat import Literal
from scvi.core.trainers import UnsupervisedTrainer
from abc import ABC, abstractmethod
from scvi.dataset import get_from_registry, transfer_anndata_setup

logger = logging.getLogger(__name__)


class VAEMixin:
    def train(
        self,
        n_epochs: int = 400,
        train_size: float = 0.9,
        test_size: Optional[float] = None,
        lr: float = 1e-3,
        n_epochs_kl_warmup: int = 400,
        n_iter_kl_warmup: int = None,
        frequency: Optional[int] = None,
        train_fun_kwargs: dict = {},
        **kwargs
    ):
        """
        Train the model.

        Parameters
        ----------
        n_epochs
            Number of passes through the dataset.
        train_size
            Size of training set in the range [0.0, 1.0].
        test_size
            Size of the test set. If `None`, defaults to 1 - `train_size`. If
            `train_size + test_size < 1`, the remaining cells belong to a validation set.
        lr
            Learning rate for optimization.
        n_epochs_kl_warmup
            Number of passes through dataset for scaling term on KL divergence to go from 0 to 1.
        n_iter_kl_warmup
            Number of minibatches for scaling term on KL divergence to go from 0 to 1.
            To use, set to not `None` and set `n_epochs_kl_warmup` to `None`.
        frequency
            Frequency with which metrics are computed on the data for train/test/val sets.
        train_fun_kwargs
            Keyword args for the train method of :class:`~scvi.core.trainers.UnsupervisedTrainer`.
        **kwargs
            Other keyword args for :class:`~scvi.core.trainers.UnsupervisedTrainer`.
        """
        train_fun_kwargs = dict(train_fun_kwargs)
        if self.is_trained_ is False:
            self.trainer = UnsupervisedTrainer(
                self.model,
                self.adata,
                train_size=train_size,
                test_size=test_size,
                n_iter_kl_warmup=n_iter_kl_warmup,
                n_epochs_kl_warmup=n_epochs_kl_warmup,
                frequency=frequency,
                use_cuda=self.use_cuda,
                **kwargs,
            )
            self.train_indices_ = self.trainer.train_set.indices
            self.test_indices_ = self.trainer.test_set.indices
            self.validation_indices_ = self.trainer.validation_set.indices
        # for autotune
        if "n_epochs" not in train_fun_kwargs:
            train_fun_kwargs["n_epochs"] = n_epochs
        if "lr" not in train_fun_kwargs:
            train_fun_kwargs["lr"] = lr
        self.trainer.train(**train_fun_kwargs)
        self.is_trained_ = True

    @torch.no_grad()
    def get_elbo(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        batch_size: int = 128,
    ) -> float:
        """
        Return the ELBO for the data.

        The ELBO is the lower bound on the log likelihood of the data used for optimization
        of VAEs.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        batch_size
            Minibatch size for data loading into model.
        """
        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)

        return -post.elbo()

    @torch.no_grad()
    def get_marginal_ll(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        n_mc_samples: int = 1000,
        batch_size: int = 128,
    ) -> float:
        """
        Return the marginal LL for the data.

        The computation here is a biased estimator of the marginal log likelihood of the data.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        batch_size
            Minibatch size for data loading into model.
        """
        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)

        return -post.marginal_ll(n_mc_samples=n_mc_samples)

    @torch.no_grad()
    def get_reconstruction_error(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        batch_size: int = 128,
    ) -> float:
        r"""
        Return the reconstruction error for the data.

        This is typically written as :math:`p(x \mid z)`, the likelihood term given one posterior sample.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        batch_size
            Minibatch size for data loading into model.
        """

        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)

        return -post.reconstruction_error()

    @torch.no_grad()
    def get_latent_representation(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        give_mean: bool = True,
        mc_samples: int = 5000,
        batch_size: int = 128,
    ) -> np.ndarray:
        """
        Return the latent representation for each cell.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        give_mean
            Give mean of distribution or sample from it.
        mc_samples
            For distributions with no closed-form mean (e.g., `logistic normal`), how many Monte Carlo
            samples to take for computing mean.
        batch_size
            Minibatch size for data loading into model.

        Returns
        -------
        latent_representation : np.ndarray
            Low-dimensional representation for each cell
        """
        if self.is_trained_ is False:
            raise RuntimeError("Please train the model first.")

        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)
        latent = []
        for tensors in post:
            x = tensors[_CONSTANTS.X_KEY]
            z = self.model.sample_from_posterior_z(
                x, give_mean=give_mean, n_samples=mc_samples
            )
            latent += [z.cpu()]
        return np.array(torch.cat(latent))


class RNASeqMixin:
    @torch.no_grad()
    def get_normalized_expression(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        transform_batch: Optional[Sequence[Union[str, int]]] = None,
        gene_list: Optional[Sequence[str]] = None,
        library_size: Union[float, Literal["latent"]] = 1,
        n_samples: int = 1,
        batch_size: int = 128,
        return_mean: bool = True,
        return_numpy: Optional[bool] = None,
    ) -> Union[np.ndarray, pd.DataFrame]:
        r"""
        Returns the normalized (decoded) gene expression.

        This is denoted as :math:`\rho_n` in the scVI paper.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        transform_batch
            Batch to condition on.
            If transform_batch is:

            - None, then real observed batch is used.
            - int, then batch transform_batch is used.
        gene_list
            Return frequencies of expression for a subset of genes.
            This can save memory when working with large datasets and few genes are
            of interest.
        library_size
            Scale the expression frequencies to a common library size.
            This allows gene expression levels to be interpreted on a common scale of relevant
            magnitude. If set to `"latent"`, use the latent libary size.
        n_samples
            Number of posterior samples to use for estimation.
        batch_size
            Minibatch size for data loading into model.
        return_mean
            Whether to return the mean of the samples.
        return_numpy
            Return a :class:`~numpy.ndarray` instead of a :class:`~pandas.DataFrame`. DataFrame includes
            gene names as columns. If either `n_samples=1` or `return_mean=True`, defaults to `False`.
            Otherwise, it defaults to `True`.

        Returns
        -------
        If `n_samples` > 1 and `return_mean` is False, then the shape is `(samples, cells, genes)`.
        Otherwise, shape is `(cells, genes)`. In this case, return type is :class:`~pandas.DataFrame` unless `return_numpy` is True.
        """
        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)
        if transform_batch is not None:
            transform_batch = _get_batch_code_from_category(adata, transform_batch)

        if gene_list is None:
            gene_mask = slice(None)
        else:
            all_genes = _get_var_names_from_setup_anndata(adata)
            gene_mask = [True if gene in gene_list else False for gene in all_genes]

        if n_samples > 1 and return_mean is False:
            if return_numpy is False:
                logger.warning(
                    "return_numpy must be True if n_samples > 1 and return_mean is False, returning np.ndarray"
                )
            return_numpy = True
        if indices is None:
            indices = np.arange(adata.n_obs)
        if library_size == "latent":
            model_fn = self.model.get_sample_rate
            scaling = 1
        else:
            model_fn = self.model.get_sample_scale
            scaling = library_size

        exprs = []
        for tensors in post:
            x = tensors[_CONSTANTS.X_KEY]
            batch_idx = tensors[_CONSTANTS.BATCH_KEY]
            labels = tensors[_CONSTANTS.LABELS_KEY]
            exprs += [
                np.array(
                    (
                        model_fn(
                            x,
                            batch_index=batch_idx,
                            y=labels,
                            n_samples=n_samples,
                            transform_batch=transform_batch,
                        )[..., gene_mask]
                        * scaling
                    ).cpu()
                )
            ]

        if n_samples > 1:
            # The -2 axis correspond to cells.
            exprs = np.concatenate(exprs, axis=-2)
        else:
            exprs = np.concatenate(exprs, axis=0)

        if n_samples > 1 and return_mean:
            exprs = exprs.mean(0)

        if return_numpy is None or return_numpy is False:
            return pd.DataFrame(
                exprs,
                columns=adata.var_names[gene_mask],
                index=adata.obs_names[indices],
            )
        else:
            return exprs

    def differential_expression(
        self,
        groupby: str,
        group1: str,
        group2: Optional[str] = None,
        adata: Optional[AnnData] = None,
        mode="vanilla",
        all_stats=True,
        use_permutation=False,
    ):
        adata = self._validate_anndata(adata)
        cell_idx1 = adata.obs[groupby] == group1
        if group2 is None:
            cell_idx2 = ~cell_idx1
        else:
            cell_idx2 = adata.obs[groupby] == group2

        model_fn = partial(self.get_normalized_expression, return_numpy=True)
        dc = DifferentialComputation(model_fn, adata)
        all_info = dc.get_bayes_factors(
            cell_idx1, cell_idx2, mode=mode, use_permutation=use_permutation
        )

        gene_names = _get_var_names_from_setup_anndata(adata)
        if all_stats is True:
            (
                mean1,
                mean2,
                nonz1,
                nonz2,
                norm_mean1,
                norm_mean2,
            ) = scrna_raw_counts_properties(adata, cell_idx1, cell_idx2)
            genes_properties_dict = dict(
                raw_mean1=mean1,
                raw_mean2=mean2,
                non_zeros_proportion1=nonz1,
                non_zeros_proportion2=nonz2,
                raw_normalized_mean1=norm_mean1,
                raw_normalized_mean2=norm_mean2,
            )
            all_info = {**all_info, **genes_properties_dict}

        res = pd.DataFrame(all_info, index=gene_names)
        sort_key = "proba_de" if mode == "change" else "bayes_factor"
        res = res.sort_values(by=sort_key, ascending=False)
        return res

    @torch.no_grad()
    def posterior_predictive_sample(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        n_samples: int = 1,
        gene_list: Optional[Sequence[str]] = None,
        batch_size: int = 128,
    ) -> np.ndarray:
        r"""
        Generate observation samples from the posterior predictive distribution.

        The posterior predictive distribution is written as :math:`p(\hat{x} \mid x)`.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        n_samples
            Number of samples for each cell.
        gene_list
            Names of genes of interest.
        batch_size
            Minibatch size for data loading into model.

        Returns
        -------
        x_new : :py:class:`torch.Tensor`
            tensor with shape (n_cells, n_genes, n_samples)
        """
        if self.model.gene_likelihood not in ["zinb", "nb", "poisson"]:
            raise ValueError("Invalid gene_likelihood.")

        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)

        if indices is None:
            indices = np.arange(adata.n_obs)

        if gene_list is None:
            gene_mask = slice(None)
        else:
            all_genes = _get_var_names_from_setup_anndata(adata)
            gene_mask = [True if gene in gene_list else False for gene in all_genes]

        x_new = []
        for tensors in post:
            x = tensors[_CONSTANTS.X_KEY]
            batch_idx = tensors[_CONSTANTS.BATCH_KEY]
            labels = tensors[_CONSTANTS.LABELS_KEY]
            outputs = self.model.inference(
                x, batch_index=batch_idx, y=labels, n_samples=n_samples
            )
            px_r = outputs["px_r"]
            px_rate = outputs["px_rate"]
            px_dropout = outputs["px_dropout"]

            if self.model.gene_likelihood == "poisson":
                l_train = px_rate
                l_train = torch.clamp(l_train, max=1e8)
                dist = torch.distributions.Poisson(
                    l_train
                )  # Shape : (n_samples, n_cells_batch, n_genes)
            elif self.model.gene_likelihood == "nb":
                dist = NegativeBinomial(mu=px_rate, theta=px_r)
            elif self.model.gene_likelihood == "zinb":
                dist = ZeroInflatedNegativeBinomial(
                    mu=px_rate, theta=px_r, zi_logits=px_dropout
                )
            else:
                raise ValueError(
                    "{} reconstruction error not handled right now".format(
                        self.model.gene_likelihood
                    )
                )
            if n_samples > 1:
                exprs = dist.sample().permute(
                    [1, 2, 0]
                )  # Shape : (n_cells_batch, n_genes, n_samples)
            else:
                exprs = dist.sample()

            if gene_list is not None:
                exprs = exprs[:, gene_mask, ...]

            x_new.append(exprs.cpu())
        x_new = torch.cat(x_new)  # Shape (n_cells, n_genes, n_samples)

        return x_new.numpy()

    @torch.no_grad()
    def _get_denoised_samples(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        n_samples: int = 25,
        batch_size: int = 64,
        rna_size_factor: int = 1000,
        transform_batch: Optional[Sequence[str]] = None,
    ) -> np.ndarray:
        """
        Return samples from an adjusted posterior predictive.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        n_samples
            Number of posterior samples to use for estimation.
        batch_size
            Minibatch size for data loading into model.
        rna_size_factor
            size factor for RNA prior to sampling gamma distribution.
        transform_batch
            int of which batch to condition on for all cells.

        Returns
        -------
        denoised_samples
        """
        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)

        posterior_list = []
        for tensors in post:
            x = tensors[_CONSTANTS.X_KEY]
            batch_idx = tensors[_CONSTANTS.BATCH_KEY]
            labels = tensors[_CONSTANTS.LABELS_KEY]

            outputs = self.model.inference(
                x, batch_index=batch_idx, y=labels, n_samples=n_samples
            )
            px_scale = outputs["px_scale"]
            px_r = outputs["px_r"]

            rate = rna_size_factor * px_scale
            if len(px_r.size()) == 2:
                px_dispersion = px_r
            else:
                px_dispersion = torch.ones_like(x) * px_r

            # This gamma is really l*w using scVI manuscript notation
            p = rate / (rate + px_dispersion)
            r = px_dispersion
            l_train = torch.distributions.Gamma(r, (1 - p) / p).sample()
            data = l_train.cpu().numpy()
            # """
            # In numpy (shape, scale) => (concentration, rate), with scale = p /(1 - p)
            # rate = (1 - p) / p  # = 1/scale # used in pytorch
            # """
            posterior_list += [data]

            posterior_list[-1] = np.transpose(posterior_list[-1], (1, 2, 0))

        return np.concatenate(posterior_list, axis=0)

    @torch.no_grad()
    def get_feature_correlation_matrix(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        n_samples: int = 10,
        batch_size: int = 64,
        rna_size_factor: int = 1000,
        transform_batch: Optional[Union[int, List[int]]] = None,
        correlation_type: Literal["spearman", "pearson"] = "spearman",
    ) -> pd.DataFrame:
        """
        Generate gene-gene correlation matrix using scvi uncertainty and expression.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        n_samples
            Number of posterior samples to use for estimation.
        batch_size
            Minibatch size for data loading into model.
        rna_size_factor
            size factor for RNA prior to sampling gamma distribution.
        transform_batch
            Batches to condition on.
            If transform_batch is:

            - None, then real observed batch is used.
            - int, then batch transform_batch is used.
            - list of int, then values are averaged over provided batches.
        correlation_type
            One of "pearson", "spearman".

        Returns
        -------
        Gene-gene correlation matrix
        """
        from scipy.stats import spearmanr

        adata = self._validate_anndata(adata)

        if (transform_batch is None) or (isinstance(transform_batch, int)):
            transform_batch = [transform_batch]
        corr_mats = []
        for b in transform_batch:
            denoised_data = self._get_denoised_samples(
                adata=adata,
                indices=indices,
                n_samples=n_samples,
                batch_size=batch_size,
                rna_size_factor=rna_size_factor,
                transform_batch=b,
            )
            flattened = np.zeros(
                (denoised_data.shape[0] * n_samples, denoised_data.shape[1])
            )
            for i in range(n_samples):
                flattened[
                    denoised_data.shape[0] * (i) : denoised_data.shape[0] * (i + 1)
                ] = denoised_data[:, :, i]
            if correlation_type == "pearson":
                corr_matrix = np.corrcoef(flattened, rowvar=False)
            elif correlation_type == "spearman":
                corr_matrix, _ = spearmanr(flattened)
            else:
                raise ValueError(
                    "Unknown correlation type. Choose one of 'spearman', 'pearson'."
                )
            corr_mats.append(corr_matrix)
        corr_matrix = np.mean(np.stack(corr_mats), axis=0)
        var_names = _get_var_names_from_setup_anndata(adata)
        return pd.DataFrame(corr_matrix, index=var_names, columns=var_names)

    @torch.no_grad()
    def get_likelihood_parameters(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        n_samples: Optional[int] = 1,
        give_mean: Optional[bool] = False,
        batch_size=128,
    ) -> Dict[str, np.ndarray]:
        r"""Estimates for the parameters of the likelihood :math:`p(x \mid z)`."""
        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)

        dropout_list = []
        mean_list = []
        dispersion_list = []
        for tensors in post:
            x = tensors[_CONSTANTS.X_KEY]
            batch_idx = tensors[_CONSTANTS.BATCH_KEY]
            labels = tensors[_CONSTANTS.LABELS_KEY]

            outputs = self.model.inference(
                x, batch_index=batch_idx, y=labels, n_samples=n_samples
            )
            px_r = outputs["px_r"]
            px_rate = outputs["px_rate"]
            px_dropout = outputs["px_dropout"]

            n_batch = px_rate.size(0) if n_samples == 1 else px_rate.size(1)
            dispersion_list += [
                np.repeat(np.array(px_r.cpu())[np.newaxis, :], n_batch, axis=0)
            ]
            mean_list += [np.array(px_rate.cpu())]
            dropout_list += [np.array(px_dropout.cpu())]

        dropout = np.concatenate(dropout_list)
        means = np.concatenate(mean_list)
        dispersions = np.concatenate(dispersion_list)
        if give_mean and n_samples > 1:
            dropout = dropout.mean(0)
            means = means.mean(0)

        return_dict = {}
        return_dict["mean"] = means

        if self.model.gene_likelihood == "zinb":
            return_dict["dropout"] = dropout
            return_dict["dispersions"] = dispersions
        if self.model.gene_likelihood == "nb":
            return_dict["dispersions"] = dispersions

        return return_dict

    @torch.no_grad()
    def get_latent_library_size(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        give_mean=True,
        batch_size=128,
    ) -> np.ndarray:
        r"""
        Returns the latent library size for each cell.

        This is denoted as :math:`\ell_n` in the scVI paper.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        give_mean
            Return the mean or a sample from the posterior distribution.
        batch_size
            Minibatch size for data loading into model.
        """
        if self.is_trained_ is False:
            raise RuntimeError("Please train the model first.")
        adata = self._validate_anndata(adata)
        post = self._make_posterior(adata=adata, indices=indices, batch_size=batch_size)
        libraries = []
        for tensors in post:
            x = tensors[_CONSTANTS.X_KEY]
            library = self.model.sample_from_posterior_l(x, give_mean=give_mean)
            libraries += [library.cpu()]
        return np.array(torch.cat(libraries))


class BaseModelClass(ABC):
    def __init__(self, adata: Optional[AnnData] = None, use_cuda=False):
        if adata is not None:
            if "_scvi" not in adata.uns.keys():
                raise ValueError(
                    "Please setup your AnnData with scvi.dataset.setup_anndata(adata) first"
                )
            self.adata = adata
            self.scvi_setup_dict_ = adata.uns["_scvi"]
            self.summary_stats = self.scvi_setup_dict_["summary_stats"]
            self._validate_anndata(adata, copy_if_view=False)

        self.is_trained_ = False
        self.use_cuda = use_cuda and torch.cuda.is_available()
        self._model_summary_string = ""
        self.train_indices_ = None
        self.test_indices_ = None
        self.validation_indices_ = None

    def _make_posterior(
        self,
        adata: AnnData,
        indices: Optional[Sequence[int]] = None,
        batch_size: int = 128,
        **posterior_kwargs
    ):
        """Create a Posterior object for data iteration."""
        if indices is None:
            indices = np.arange(adata.n_obs)
        post = self._posterior_class(
            self.model,
            adata,
            shuffle=False,
            indices=indices,
            use_cuda=self.use_cuda,
            batch_size=batch_size,
            **posterior_kwargs,
        ).sequential()
        return post

    def _validate_anndata(
        self, adata: Optional[AnnData] = None, copy_if_view: bool = True
    ):
        """Validate anndata has been properly registered, transfer if necessary."""
        if adata is None:
            adata = self.adata
        if adata.is_view:
            logger.warning("Input anndata is a view.")
            if copy_if_view:
                logger.info("Making copy of anndata.")
                adata = adata.copy()
        if "_scvi" not in adata.uns_keys():
            logger.info(
                "Input adata not setup with scvi. "
                + "attempting to transfer anndata setup"
            )
            transfer_anndata_setup(self.scvi_setup_dict_, adata)
        is_nonneg_int = _check_nonnegative_integers(
            get_from_registry(adata, _CONSTANTS.X_KEY)
        )
        if not is_nonneg_int:
            logger.warning(
                "Make sure the registered X field in anndata contains unnormalized count data."
            )

        _check_anndata_setup_equivalence(self.scvi_setup_dict_, adata)

        return adata

    @property
    @abstractmethod
    def _posterior_class(self):
        pass

    @property
    @abstractmethod
    def _trainer_class(self):
        pass

    @abstractmethod
    def train(self):
        pass

    @property
    def is_trained(self):
        return self.is_trained_

    @property
    def test_indices(self):
        return self.test_indices_

    @property
    def train_indices(self):
        return self.train_indices_

    @property
    def validation_indices(self):
        return self.validation_indices_

    def _get_user_attributes(self):
        # returns all the self attributes defined in a model class, eg, self.is_trained_
        attributes = inspect.getmembers(self, lambda a: not (inspect.isroutine(a)))
        attributes = [
            a for a in attributes if not (a[0].startswith("__") and a[0].endswith("__"))
        ]
        attributes = [a for a in attributes if not a[0].startswith("_abc_")]
        return attributes

    def _get_init_params(self, locals):
        # returns the model init signiture with associated passed in values
        # except the anndata objects passed in
        init = self.__init__
        sig = inspect.signature(init)
        init_params = [p for p in sig.parameters]
        user_params = {p: locals[p] for p in locals if p in init_params}
        user_params = {
            k: v for (k, v) in user_params.items() if not isinstance(v, AnnData)
        }
        return user_params

    def save(self, dir_path: str, overwrite: bool = False):
        """
        Save the state of the `scvi` model.

        The trainer optimizer state nor the trainer history are saved.

        Parameters
        ----------
        dir_path
            Path to a directory.
        overwrite
            Overwrite existing data or not. If `False` and directory
            already exists at `dir_path`, error will be raised.
        """
        # get all the user attributes
        user_attributes = self._get_user_attributes()
        # only save the public attributes with _ at the very end
        user_attributes = {a[0]: a[1] for a in user_attributes if a[0][-1] == "_"}
        # save the model state dict and the trainer state dict only
        if not os.path.exists(dir_path) or overwrite:
            os.makedirs(dir_path, exist_ok=overwrite)
        else:
            raise ValueError(
                "{} already exists. Please provide an unexisting directory for saving.".format(
                    dir_path
                )
            )
        torch.save(self.model.state_dict(), os.path.join(dir_path, "model_params.pt"))
        with open(os.path.join(dir_path, "attr.pkl"), "wb") as f:
            pickle.dump(user_attributes, f)

    @classmethod
    def load(cls, adata: AnnData, dir_path: str, use_cuda: bool = False):
        """
        Create an `scvi` model from the saved output.

        Parameters
        ----------
        adata
            AnnData organized in the same way as data used to train model.
            It is not necessary to run :func:`~scvi.dataset.setup_anndata`,
            as AnnData is validated against the saved `scvi` setup dictionary.
        dir_path
            Path to saved outputs.
        use_cuda
            Whether to load model on GPU.

        Returns
        -------
        `scvi` model

        Examples
        --------
        >>> vae = SCVI.load(adata, save_path)
        >>> vae.get_latent_representation()
        """
        model_path = os.path.join(dir_path, "model_params.pt")
        setup_dict_path = os.path.join(dir_path, "attr.pkl")
        with open(setup_dict_path, "rb") as handle:
            attr_dict = pickle.load(handle)
        if "init_params_" not in attr_dict.keys():
            raise ValueError(
                "No init_params_ were saved by the model. Check out the developers guide if creating custom models."
            )
        # get the parameters for the class init signiture
        init_params = attr_dict.pop("init_params_")
        # grab all the parameters execept for kwargs (is a dict)
        non_kwargs = {k: v for k, v in init_params.items() if not isinstance(v, dict)}
        # expand out kwargs
        kwargs = {k: v for k, v in init_params.items() if isinstance(v, dict)}
        kwargs = {k: v for (i, j) in kwargs.items() for (k, v) in j.items()}
        model = cls(adata, **non_kwargs, **kwargs)
        for attr, val in attr_dict.items():
            setattr(model, attr, val)
        use_cuda = use_cuda and torch.cuda.is_available()

        if use_cuda:
            model.model.load_state_dict(torch.load(model_path))
            model.model.cuda()
        else:
            device = torch.device("cpu")
            model.model.load_state_dict(torch.load(model_path, map_location=device))
        model.model.eval()
        model._validate_anndata(adata)
        return model

    def __repr__(
        self,
    ):
        summary_string = self._model_summary_string + "\nTraining status: {}".format(
            "Trained" if self.is_trained_ else "Not Trained"
        )
        return summary_string
