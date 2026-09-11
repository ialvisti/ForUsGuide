"""Read-only semantic replay support for explicitly simulated PA fixtures.

The caller supplies an engine with the verified deployed configuration. No ticket
IDs, reviews or human answers are passed to the engine; evidence stays local.
These replays never invoke ticket endpoints, scrapers, publication or indexing.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from data_pipeline.gr_payload_builder import build_collected_data


def case_collected_data(case: dict[str, Any]) -> dict[str, Any]:
    data = case['test_input']
    collected = build_collected_data(
        data.get('participant_modules'), data.get('plan_modules'), data.get('ticket_extracted'),
    )
    collected['internal_response_context'] = {
        'requested_questions': case['requested_questions'],
        'intent_source': 'sanitized_simulation',
    }
    return collected


def source_manifest(root: Path) -> dict[str, str]:
    paths = sorted([*root.glob('data_pipeline/*.py'), *root.glob('api/*.py')])
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


async def run_replays(engine: Any, fixture: dict[str, Any], output_dir: Path,
                      provenance: dict[str, Any], *, concurrency: int = 2,
                      max_response_tokens: int = 5500) -> list[dict[str, Any]]:
    if fixture.get('count') != len(fixture['cases']):
        raise ValueError('Fixture count mismatch')
    if not 1 <= concurrency <= 3:
        raise ValueError('Replay concurrency must be 1-3')
    if any(case.get('fixture_kind') != 'simulation' for case in fixture['cases']):
        raise ValueError('This runner accepts explicitly sanitized simulations only')
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    limiter = asyncio.Semaphore(concurrency)

    async def run(case: dict[str, Any]) -> dict[str, Any]:
        async with limiter:
            started = datetime.now(timezone.utc).isoformat()
            settings = case['test_input']
            result: dict[str, Any] = {'ticket_id': case['ticket_id'], 'review_id': case['review_id'],
                                      'fixture_kind': 'simulation', 'historical_cutoff': case['historical_cutoff'],
                                      'started_at': started, 'provenance': provenance,
                                      'source_conditions': case['remaining_source_conditions'],
                                      'semantic_review': 'pending'}
            try:
                if settings['route'] == 'knowledge_question':
                    response = await engine.ask_knowledge_question(
                        question=case['inquiry'],
                        **({'identity_context': settings['identity_context']} if settings.get('identity_context') else {}),
                    )
                else:
                    result['request_parameters'] = {'max_response_tokens': max_response_tokens}
                    if case['scenario'] == 'hardship':
                        required = await engine.get_required_data(
                            inquiry=case['inquiry'], record_keeper=settings['record_keeper'],
                            plan_type=settings['plan_type'], topic=settings['topic'],
                        )
                        result['required_data'] = asdict(required)
                    collected = case_collected_data(case)
                    result['collected_data'] = collected
                    response = await engine.generate_response(
                        inquiry=case['inquiry'], record_keeper=settings['record_keeper'],
                        plan_type=settings['plan_type'], topic=settings['topic'],
                        collected_data=collected, max_response_tokens=max_response_tokens,
                    )
                result['result'] = asdict(response)
                result['execution_error'] = response.metadata.get('error')
            except Exception as exc:
                result['execution_error'] = type(exc).__name__
            result['finished_at'] = datetime.now(timezone.utc).isoformat()
            (output_dir / (case['ticket_id'] + '.json')).write_text(json.dumps(result, indent=2, default=str) + '\n')
            print(json.dumps({'ticket': case['ticket_id'], 'completed': True,
                              'error': result.get('execution_error')}), flush=True)
            return result

    results = await asyncio.gather(*(run(case) for case in fixture['cases']))
    summary = {'scope_sha256': fixture['scope_sha256'], 'provenance': provenance,
               'count': len(results), 'execution_errors': sum(bool(r.get('execution_error')) for r in results),
               'semantic_review': 'pending', 'ticket_writes': 0, 'index_writes': 0}
    (output_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return results
