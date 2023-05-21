"""Tools for running sweeps over hyperparameters."""
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Sequence

from src import data, functional, metrics, models, operators
from src.utils import experiment_utils, tokenizer_utils
from src.utils.typing import PathLike, StrSequence

import numpy as np
import torch
from dataclasses_json import DataClassJsonMixin

logger = logging.getLogger(__name__)

DEFAULT_RECALL_K = 3
DEFAULT_N_TRIALS = 3
DEFAULT_N_TRAIN_SAMPLES = 5
DEFAULT_BATCH_SIZE = 64


@dataclass(frozen=True)
class SweepFaithfulnessBetaResults(DataClassJsonMixin):
    beta: float
    recall: list[float]


@dataclass(frozen=True)
class SweepFaithfulnessTrainResults(DataClassJsonMixin):
    samples: list[data.RelationSample]
    betas: list[SweepFaithfulnessBetaResults]

    def best(self, k: int = 1) -> SweepFaithfulnessBetaResults:
        """Return the best beta by given recall position."""
        return max(self.betas, key=lambda x: x.recall[k - 1])

    def summarize(self) -> None:
        """Sumarize results in debug logs."""
        best = self.best()
        logger.debug(
            f"beta={best.beta:.2f} | recall@1={best.recall[0]:.2f} | samples={[str(x) for x in self.samples]}"
        )


@dataclass(frozen=True)
class SweepFaithfulnessLayerResults(DataClassJsonMixin):
    layer: int
    result: SweepFaithfulnessTrainResults


@dataclass(frozen=True)
class SweepFaithfulnessTrialResults(DataClassJsonMixin):
    prompt_template: str
    train_samples: list[data.RelationSample]
    layers: list[SweepFaithfulnessLayerResults]


@dataclass(frozen=True)
class SweepFaithfulnessLayerSummary(DataClassJsonMixin):
    layer: int
    beta: metrics.AggregateMetric
    recall: metrics.AggregateMetric


@dataclass(frozen=True)
class SweepFaithfulnessRelationResults(DataClassJsonMixin):
    relation_name: str
    trials: list[SweepFaithfulnessTrialResults]

    def by_layer(self, k: int = 1) -> dict[int, SweepFaithfulnessLayerSummary]:
        """Return best layer and average beta for that layer."""
        results_by_layer = defaultdict(list)
        for trial in self.trials:
            for layer in trial.layers:
                best = layer.result.best()
                results_by_layer[layer.layer].append(
                    (
                        layer.layer,
                        best.beta,
                        best.recall[k - 1],
                    )
                )

        recalls_by_layer = {
            layer: metrics.AggregateMetric.aggregate([x[-1] for x in results])
            for layer, results in results_by_layer.items()
        }
        betas_by_layer = {
            layer: metrics.AggregateMetric.aggregate([x[1] for x in results])
            for layer, results in results_by_layer.items()
        }
        return {
            layer: SweepFaithfulnessLayerSummary(
                layer=layer,
                beta=betas_by_layer[layer],
                recall=recalls_by_layer[layer],
            )
            for layer in recalls_by_layer
        }

    def best(self, k: int = 1) -> SweepFaithfulnessLayerSummary:
        """Return the best layer and average beta for that layer."""
        results_by_layer = self.by_layer()
        best_layer = max(
            results_by_layer, key=lambda x: results_by_layer[x].recall.mean
        )
        return results_by_layer[best_layer]

    def summarize(self, k: int = 1) -> None:
        """Print a summary of what happened."""
        results_by_layer = self.by_layer(k=k)
        logger.debug(f'summarizing results for "{self.relation_name}"')
        for la, summ in results_by_layer.items():
            logger.debug(f"layer={la} | beta={summ.beta} | recall@{k}={summ.recall}")


@dataclass(frozen=True)
class SweepFaithfulnessResuts(DataClassJsonMixin):
    relations: list[SweepFaithfulnessRelationResults]


def sweep_faithfulness(
    *,
    mt: models.ModelAndTokenizer,
    dataset: data.RelationDataset,
    h_layers: Sequence[int] | None = None,
    betas: Sequence[float] | None = None,
    n_trials: int = DEFAULT_N_TRIALS,
    n_train_samples: int = DEFAULT_N_TRAIN_SAMPLES,
    recall_k: int = DEFAULT_RECALL_K,
    batch_size: int = DEFAULT_BATCH_SIZE,
    results_dir: PathLike | None = None,
    resume: bool = False,
    **kwargs: Any,
) -> SweepFaithfulnessResuts:
    """Sweep over hyperparameters for faithfulness."""
    if h_layers is None:
        h_layers = models.determine_layers(mt)
    if betas is None:
        betas = torch.linspace(0, 1, steps=21).tolist()
    logger.info("begin sweeping faithfulness")

    relation_results = []
    for ri, relation in enumerate(dataset.relations):
        logger.info(
            f'begin relation "{relation.name}" ({ri + 1}/{len(dataset.relations)})'
        )

        relation_result = experiment_utils.load_results_file(
            results_dir=results_dir,
            results_type=SweepFaithfulnessRelationResults,
            name=relation.name,
            resume=resume,
        )
        if relation_result is not None:
            logger.info(f"loaded previous results for {relation.name}")
            relation_results.append(relation_result)
            continue

        prompt_template = relation.prompt_templates[0]

        trial_results = []
        for trial in range(n_trials):
            logger.info(f"begin trial {trial + 1}/{n_trials}")

            if len(relation.samples) <= n_train_samples:
                logger.warning(
                    f"Not enough samples ({len(relation.samples)}) to "
                    f'test for "{relation.name} with n_train_samples={n_train_samples}.'
                    f"You should fix this by adding more known samples for the relation."
                )
                continue

            # Decide which will be the train samples we will try, and which will be the
            # ICL prompt examples.
            train_relation, test_relation = relation.split(n_train_samples)
            train_samples = train_relation.samples
            train_icl_samples = train_samples[:-1]

            logger.info(f"will train using: {[str(x) for x in train_samples]}")
            logger.info(
                f"will do icl for testing using: {[str(x) for x in train_icl_samples]}"
            )

            # Precompute all the hs to speed things up.
            hs_by_subj = _precompute_hs(
                mt=mt,
                prompt_template=prompt_template,
                subjects=[x.subject for x in relation.samples],
                batch_size=batch_size,
                examples=train_icl_samples,
            )

            layer_results = []
            for h_layer in h_layers:
                logger.info(f"begin layer: {h_layer}")

                estimator = operators.JacobianIclMeanEstimator(
                    mt=mt, h_layer=h_layer, **kwargs
                )

                operator = estimator(
                    relation.set(
                        samples=train_samples,
                        prompt_templates=[prompt_template],
                    )
                )
                assert operator.bias is not None
                bias = operator.bias.clone()

                test_samples = test_relation.samples
                test_subjects = [x.subject for x in test_samples]
                test_hs = [hs_by_subj[x.subject][h_layer, None] for x in test_samples]
                test_objects = [x.object for x in test_samples]

                results_by_beta = []
                recalls_by_beta = []
                for beta in betas:
                    operator.bias[:] = bias * beta

                    pred_objects = []
                    for subj, h in zip(test_subjects, test_hs):
                        preds = operator(subj, h=h, k=recall_k)
                        pred_objects.append([p.token for p in preds.predictions])

                    recall = metrics.recall(pred_objects, test_objects)
                    recalls_by_beta.append(recall)
                    results_by_beta.append(
                        SweepFaithfulnessBetaResults(beta=beta, recall=recall)
                    )

                train_result = SweepFaithfulnessTrainResults(
                    samples=train_samples, betas=results_by_beta
                )
                train_result.summarize()
                layer_results.append(
                    SweepFaithfulnessLayerResults(layer=h_layer, result=train_result)
                )
            trial_results.append(
                SweepFaithfulnessTrialResults(
                    prompt_template=prompt_template,
                    train_samples=train_samples,
                    layers=layer_results,
                )
            )
        relation_result = SweepFaithfulnessRelationResults(
            relation_name=relation.name, trials=trial_results
        )
        relation_result.summarize()
        experiment_utils.save_results_file(
            results_dir=results_dir,
            results=relation_result,
            name=relation.name,
        )
        relation_results.append(relation_result)
    return SweepFaithfulnessResuts(relation_results)


def _precompute_hs(
    *,
    mt: models.ModelAndTokenizer,
    prompt_template: str,
    subjects: StrSequence,
    examples: Sequence[data.RelationSample] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, torch.Tensor]:
    """Precompute h for every subject at every layer."""
    prompts = [
        functional.make_prompt(
            mt=mt,
            prompt_template=prompt_template,
            subject=subject,
            examples=examples,
        )
        for subject in subjects
    ]
    inputs = mt.tokenizer(
        prompts,
        return_tensors="pt",
        padding="longest",
        return_offsets_mapping=True,
    )
    offset_mapping = inputs.pop("offset_mapping")

    batched_hidden_states = []
    for i in range(0, len(inputs.input_ids), batch_size):
        with torch.inference_mode():
            outputs = mt.model(
                inputs.input_ids[i : i + batch_size],
                attention_mask=inputs.attention_mask[i : i + batch_size],
                output_hidden_states=True,
                return_dict=True,
            )
        batched_hidden_states.append(torch.stack(outputs.hidden_states)[1:])
    hidden_states = torch.cat(batched_hidden_states, dim=1)

    hs_by_subj = {}
    for i, (subject, prompt) in enumerate(zip(subjects, prompts)):
        _, h_index = tokenizer_utils.find_token_range(
            prompt, subject, offset_mapping=offset_mapping[i]
        )
        h_index -= 1
        hs_by_subj[subject] = hidden_states[:, i, h_index]

    return hs_by_subj
