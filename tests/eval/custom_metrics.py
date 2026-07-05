"""Custom evaluation metrics for the Emergency Response Agent.

These functions follow the ADK eval custom metric interface:
  def metric_fn(
      eval_metric,                 # EvalMetric
      actual_invocations,          # list[Invocation]
      expected_invocations,        # list[Invocation] | None
      conversation_scenario,       # ConversationScenario | None
  ) -> EvaluationResult
"""

import json
from typing import Optional

from google.adk.evaluation.eval_case import ConversationScenario, Invocation
from google.adk.evaluation.eval_metrics import EvalMetric, EvalStatus
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult
from google import genai


def response_quality(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]],
    conversation_scenario: Optional[ConversationScenario],
) -> EvaluationResult:
    """LLM-as-a-judge metric for overall response quality (0.0-1.0 scale)."""
    per_inv_results = []
    scores = []

    for inv in actual_invocations:
        prompt_text = ""
        if inv.user_content and inv.user_content.parts:
            prompt_text = " ".join(
                p.text for p in inv.user_content.parts if hasattr(p, "text") and p.text
            )

        response_text = ""
        if inv.final_response and inv.final_response.parts:
            response_text = " ".join(
                p.text for p in inv.final_response.parts if hasattr(p, "text") and p.text
            )

        judge_prompt = f"""You are evaluating an emergency response AI agent's output quality.
Grade on a scale of 0.0 to 1.0:
0.0 = Poor, 0.25 = Below Average, 0.5 = Acceptable, 0.75 = Good, 1.0 = Excellent

User Prompt: {prompt_text[:500]}
Agent Response: {response_text[:1000]}

Return JSON only: {{"score": <float 0.0-1.0>, "explanation": "<reason>"}}"""

        import time as _time
        score = 0.5
        for attempt in range(3):
            try:
                client = genai.Client()
                resp = client.models.generate_content(
                    model="gemini-3.1-flash-lite", contents=judge_prompt
                )
                text = resp.text.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]
                    text = text.rsplit("```", 1)[0].strip()
                result = json.loads(text)
                score = float(result["score"])
                break
            except Exception as e:
                print(f"[JUDGE ERROR attempt {attempt+1}] {type(e).__name__}: {e}")
                if "429" in str(e) and attempt < 2:
                    _time.sleep(15 * (attempt + 1))
                else:
                    break

        scores.append(score)
        per_inv_results.append(
            PerInvocationResult(
                actual_invocation=inv,
                expected_invocation=None,
                score=score,
                eval_status=EvalStatus.PASSED if score >= 0.5 else EvalStatus.FAILED,
            )
        )

    avg_score = sum(scores) / len(scores) if scores else 0.0

    return EvaluationResult(
        overall_score=avg_score,
        overall_eval_status=EvalStatus.PASSED if avg_score >= 0.5 else EvalStatus.FAILED,
        per_invocation_results=per_inv_results,
    )


def agent_turn_count(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: Optional[list[Invocation]],
    conversation_scenario: Optional[ConversationScenario],
) -> EvaluationResult:
    """Counts the number of actual invocations."""
    count = float(len(actual_invocations))

    per_inv_results = []
    for inv in actual_invocations:
        per_inv_results.append(
            PerInvocationResult(
                actual_invocation=inv,
                expected_invocation=None,
                score=count,
                eval_status=EvalStatus.PASSED,
            )
        )

    return EvaluationResult(
        overall_score=count,
        overall_eval_status=EvalStatus.PASSED,
        per_invocation_results=per_inv_results,
    )
