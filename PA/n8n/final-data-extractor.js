/**
 * PRIVATE unpublished n8n Code-node candidate. NOT APPLIED. No require().
 * Inlines v3 guard + adapter + frozen e1 tail (sha d18792a1... except the parse line).
 * Paste would replace Data extractor jsCode; this lane does not paste or publish.
 */
'use strict';
/**
 * PRIVATE candidate v3 (class-fix lane): v2 plus two additive fail-closed
 * post-checks on the accept path. No new rewrite power, no new facts.
 * Check A: residual will|shall|you are subject to|is (applied|assessed|charged|
 * imposed|deducted|withheld|owed|due) in a penalty-scope sentence of the
 * accepted reply (CG1 class; possibility elsewhere cannot mask it).
 * Check B: second-person age assertion in a non-hypothetical sentence when the
 * reply has penalty scope (CG3 class). If/when/unless/should/whether + you stay.
 * Does not convert asserted age into a hypothetical.
 *
 * NOT a tax interpreter. NOT publication. Does not remove the JSON-parser LLM.
 */

const KEYS = ['ticketId', 'participant_reply', 'set_stage_solved', 'stage_reason', 'internal_notes'];

const PENALTY_SCOPE = [
  /early[\s-]?withdrawal\s+penalt/i,
  /penalt\w*\s+(?:for|on)\s+early\s+withdrawal/i,
  /\b10\s?%[^.\n]{0,60}penalt/i,
  /penalt\w*[^.\n]{0,60}\b10\s?%/i,
  /\badditional\s+tax\b[^.\n]{0,40}\b10\s?%/i,
  /\b10\s?%[^.\n]{0,60}early\s+withdrawal/i,
  /early\s+withdrawal[^.\n]{0,60}\b10\s?%/i,
];

const CERTAINTY = [
  /\b(?:will|would|shall)\s+(?:also\s+|still\s+|then\s+)?(?:be\s+)?(?:appl\w+|assess\w*|charg\w*|impos\w*|owe\w*|add\w*|incur\w*|trigger\w*)/i,
  /\byou\s+(?:will|would)\s+(?:owe|pay|incur|be\b)/i,
  /\byou\s+are\s+subject\s+to\b/i,
  /\bis\s+(?:applied|assessed|charged|imposed|added|owed|due)\b/i,
  /\b(?:a|an|the|this)\b[^.\n]{0,60}penalt\w*\s+(?:applies|attaches)\b/i,
  /\bthere\s+is\s+(?:a|an)\b[^.\n]{0,40}penalt/i,
  /\bexpect\s+(?:a|an)\b[^.\n]{0,40}penalt/i,
];

const POSSIBILITY = [
  /\b(?:may|might|could)\s+(?:also\s+|still\s+|then\s+)?(?:be\b|appl\w+|owe\w*|result\w*|incur\w*|trigger\w*|add\w*|assess\w*|charg\w*|impos\w*)/i,
  /\b(?:possible|potential(?:ly)?|generally|typically|usually|often)\b/i,
  /\bcan\s+(?:appl\w+|be\s+(?:assessed|charged|imposed|owed))/i,
  /\bwhether\b[^.\n]{0,60}\b(?:appl\w+|owe\w*)/i,
  /\bsubject\s+to\s+(?:a\s+)?possible\b/i,
  /\bnot\s+(?:necessarily|always)\b/i,
];

const CIRCUMSTANCE = [
  /\bif\s+(?:you|the\s+participant)\b/i,
  /\bwhen\s+(?:you|the\s+participant)\s+(?:are|is)\b/i,
  /\bdepend(?:s|ing)\s+on\s+(?:your|the\s+participant)/i,
];

const ASSERTED_PARTICIPANT_AGE = [
  /\bbecause\s+you\s+are\b/i,
  /\bsince\s+you\s+are\b/i,
  /\bas\s+you\s+are\s+under\b/i,
  /\bgiven\s+(?:that\s+)?you\s+are\b/i,
  /\bdue\s+to\s+your\s+age\b/i,
];

const AGE_THRESHOLD = /\b59\s?(?:1\/2|½|\.5)?\b/;
const SAFE_APPLY = /\b(?:will|would|shall)((?:\s+(?:also|still|then))*)\s+(?:be\s+)?(apply|applies|applied)\b/gi;
// Additive accept-path post-checks (Opus prototype). Fail closed; do not rewrite.
const RESIDUAL_CERTAIN =
  /\b(?:will|shall|would)\b|\byou\s+are\s+subject\s+to\b|\bis\s+(?:applied|assessed|charged|imposed|deducted|withheld|owed|due)\b/i;
const AGE_ASSERTION =
  /\byou(?:'re|r)?\b[^.\n]{0,40}\b(?:are|were|is|turn|turned|have\s+not\s+yet\s+turned|age\s+of|under\s+age)\b[^.\n]{0,40}\b(?:age\s+)?(?:\d{2}|59\s?(?:1\/2|½|\.5))\b/i;
const HYPOTHETICAL_AGE_ASSERTION =
  /\b(?:if|when|unless|should|whether)\s+you(?:'re|r)?\b[^.\n]{0,40}\b(?:are|were|is|turn|turned|have\s+not\s+yet\s+turned|age\s+of|under\s+age)\b[^.\n]{0,40}\b(?:age\s+)?(?:\d{2}|59\s?(?:1\/2|½|\.5))\b/gi;

function any(pats, s) {
  return pats.filter((p) => p.test(s));
}

function sentences(text) {
  return String(text || '')
    .split(/(?<=[.!?])\s+|\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function inPenaltyScope(s) {
  return PENALTY_SCOPE.some((p) => p.test(s));
}

function hasUnsupportedAssertedAge(sentence) {
  return ASSERTED_PARTICIPANT_AGE.some((p) => p.test(sentence));
}

function hasResidualCategorical(sentence) {
  return RESIDUAL_CERTAIN.test(sentence);
}

function hasNonHypotheticalAgeAssertion(sentence) {
  HYPOTHETICAL_AGE_ASSERTION.lastIndex = 0;
  const governed = String(sentence).replace(HYPOTHETICAL_AGE_ASSERTION, ' ');
  HYPOTHETICAL_AGE_ASSERTION.lastIndex = 0;
  return AGE_ASSERTION.test(governed);
}

function acceptPathPostCheck(reply, audit) {
  const parts = sentences(reply);
  const penaltyHit = parts.some(inPenaltyScope) || inPenaltyScope(reply);
  for (const s of parts) {
    if (inPenaltyScope(s) && hasResidualCategorical(s)) {
      audit.push({ action: 'fail_closed_residual_categorical' });
      return { fail: true, reason: 'residual_categorical' };
    }
  }
  if (penaltyHit) {
    for (const s of parts) {
      if (hasNonHypotheticalAgeAssertion(s)) {
        audit.push({ action: 'fail_closed_unsupported_participant_age' });
        return { fail: true, reason: 'unsupported_participant_age' };
      }
    }
  }
  return { fail: false };
}

function classify(sentence) {
  const cert = any(CERTAINTY, sentence);
  const poss = any(POSSIBILITY, sentence);
  const circ = any(CIRCUMSTANCE, sentence);
  let verdict;
  // Narrow syntactic rule: matched certainty is not overridden by possibility
  // elsewhere in the sentence (exception "may apply" does not clear "would apply").
  if (cert.length) verdict = 'CATEGORICAL';
  else if (poss.length) verdict = 'CONDITIONAL';
  else if (circ.length) verdict = 'CONDITIONAL';
  else verdict = 'UNDETERMINED';
  return { sentence, verdict };
}

function numbersAndNames(s) {
  return {
    dollars: [...String(s).matchAll(/\$\s?[\d,]+(?:\.\d+)?/g)].map((m) => m[0]),
    percents: [...String(s).matchAll(/\b\d+(?:\.\d+)?\s?%/g)].map((m) => m[0]),
    ages: AGE_THRESHOLD.test(s),
  };
}

function factsGrew(before, after) {
  const a = numbersAndNames(before);
  const b = numbersAndNames(after);
  if (b.dollars.length > a.dollars.length) return true;
  if (b.percents.length > a.percents.length) return true;
  if (b.ages && !a.ages) return true;
  return false;
}

function safeRewrite(sentence) {
  if (hasUnsupportedAssertedAge(sentence)) return null;
  SAFE_APPLY.lastIndex = 0;
  if (!SAFE_APPLY.test(sentence)) return null;
  SAFE_APPLY.lastIndex = 0;
  const next = sentence.replace(SAFE_APPLY, 'may$1 apply');
  SAFE_APPLY.lastIndex = 0;
  if (next === sentence) return null;
  if (classify(next).verdict === 'CATEGORICAL') return null;
  if (factsGrew(sentence, next)) return null;
  return next;
}

function serialize(obj) {
  const ordered = {};
  for (const k of KEYS) ordered[k] = obj[k];
  return JSON.stringify(ordered);
}

function contractOk(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false;
  const keys = Object.keys(parsed);
  if (keys.length !== KEYS.length || KEYS.some((k) => !Object.prototype.hasOwnProperty.call(parsed, k))) {
    return false;
  }
  if (typeof parsed.participant_reply !== 'string') return false;
  if (typeof parsed.stage_reason !== 'string') return false;
  if (typeof parsed.set_stage_solved !== 'boolean') return false;
  if (!(parsed.internal_notes === null || typeof parsed.internal_notes === 'string')) return false;
  if (typeof parsed.ticketId !== 'string') return false;
  return true;
}

function failClosed(parsed, audit, reason) {
  return {
    ok: false,
    status: 'uncertain',
    reason: reason || 'ambiguous_or_unrewritable_penalty_sentence',
    output: serialize(parsed),
    audit,
  };
}

function guardParserOutput(raw) {
  const original = String(raw ?? '');
  let parsed;
  try {
    parsed = JSON.parse(original);
  } catch (err) {
    return {
      ok: false,
      status: 'unsafe',
      reason: 'malformed_json',
      error: err && err.message ? err.message : 'parse_failed',
      output: original,
      audit: [],
    };
  }
  if (!contractOk(parsed)) {
    return {
      ok: false,
      status: 'unsafe',
      reason: 'invalid_contract',
      output: original,
      audit: [],
    };
  }

  const reply = parsed.participant_reply;
  const hits = sentences(reply).filter(inPenaltyScope);
  const audit = [];
  const replacements = [];
  let uncertain = false;

  for (const sentence of hits) {
    if (hasUnsupportedAssertedAge(sentence)) {
      uncertain = true;
      audit.push({ action: 'fail_closed_asserted_participant_age' });
      continue;
    }
    const c = classify(sentence);
    if (c.verdict === 'CONDITIONAL') {
      audit.push({ action: 'preserve_conditional' });
      continue;
    }
    if (c.verdict === 'CATEGORICAL') {
      const next = safeRewrite(sentence);
      if (!next) {
        uncertain = true;
        audit.push({ action: 'fail_closed_unrewritable_categorical' });
        continue;
      }
      replacements.push({ from: sentence, to: next });
      audit.push({ action: 'narrow_would_apply_to_may_apply' });
      continue;
    }
    uncertain = true;
    audit.push({ action: 'fail_closed_ambiguous', verdict: c.verdict });
  }

  if (uncertain) {
    return failClosed(parsed, audit);
  }

  let nextReply = reply;
  for (const r of replacements) {
    const idx = nextReply.indexOf(r.from);
    if (idx < 0) {
      return failClosed(parsed, audit, 'sentence_anchor_missed');
    }
    nextReply = nextReply.slice(0, idx) + r.to + nextReply.slice(idx + r.from.length);
  }

  const post = acceptPathPostCheck(nextReply, audit);
  if (post.fail) {
    return failClosed(parsed, audit, post.reason);
  }

  const outObj = {
    ticketId: parsed.ticketId,
    participant_reply: nextReply,
    set_stage_solved: parsed.set_stage_solved,
    stage_reason: parsed.stage_reason,
    internal_notes: parsed.internal_notes,
  };

  return {
    ok: true,
    status: replacements.length ? 'transformed' : 'unchanged',
    output: serialize(outObj),
    audit,
  };
}


const PUBLIC_REJECT = 'PA certainty guard rejected parser output; no ticket write permitted';
const EMPLOYMENT_REJECT = 'Unsupported employment-status claim; no ticket write permitted';
// The canonical fact allowlist has no employment-status source, so no current
// evidence object authorizes an exact current status or employment date.
// Conditional employer actions ("to show a terminated status", "already has a
// termination date on file") are procedures, not this participant's current fact.
const EMPLOYMENT_STATUS_WORD = 'active|terminated|retired|rehired|deceased|on leave';
const EMPLOYMENT_DATE_TEXT = '\\d{4}-\\d{2}-\\d{2}|\\d{1,2}[/-]\\d{1,2}[/-]\\d{2,4}|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\\s+\\d{1,2},?\\s+\\d{4}';

// A status term inside the condition itself is a procedure, so the protasis is
// dropped before the status rules run. Text after the condition resolves stays
// an assertion ("If you ask, our records show Active"), so this is not a blanket
// "if" exemption. The date rules keep the whole sentence and their own step test.
function assertedPortion(text) {
  const lead = text.replace(/^[\s*_>-]+/, '');
  if (!/^if\b/i.test(lead)) return text;
  const comma = lead.indexOf(',');
  // Without a comma there is no apodosis boundary to trust, so the whole
  // sentence stays under test: "If you ask we can confirm our records show
  // Active." is an assertion, not a condition.
  return comma === -1 ? text : lead.slice(comma + 1);
}

function currentPersonalEmploymentClaim(sentence) {
  const text = String(sentence || '');
  const asserted = assertedPortion(text);
  const status = `(?:${EMPLOYMENT_STATUS_WORD})`;
  if (new RegExp(`\\bemployment status\\b[^.]{0,40}\\b(?:as|is|of|:)?\\s*${status}\\b`, 'i').test(asserted)) return true;
  if (new RegExp(`\\bemployment record\\s+is\\s+${status}\\b`, 'i').test(asserted)) return true;
  if (new RegExp(`(?:^|[\\s.;])status\\s*:\\s*${status}\\b`, 'i').test(asserted)) return true;
  if (new RegExp(`\\byour\\s+status\\s+(?:is|as)\\s+${status}\\b`, 'i').test(asserted)) return true;
  if (/\b(?:you|your)\b[^.]{0,40}\bremain(?:s)? employed\b/i.test(asserted)) return true;
  if (new RegExp(`\\b(?:records?|payroll|system)\\b[^.]{0,50}\\b(?:show|shows)\\b[^.]{0,40}\\b(?:you|your)\\b[^.]{0,30}\\b(?:employed|${EMPLOYMENT_STATUS_WORD})\\b`, 'i').test(asserted)) return true;
  // A record can report a status with verbs other than "show". "has" stays out:
  // the accepted route says the record "already has a termination date on file".
  if (new RegExp(`\\brecords?\\b(?!\\s*(?:\\*\\*)?\\s*to\\s+show\\b)[^.]{0,40}\\b(?:show|shows|list|lists|reflect|reflects|indicate|indicates|is)\\b[^.]{0,24}\\b${status}\\b`, 'i').test(asserted)) return true;
  const statesMissingDate = /\b(?:no|without(?:\s+a)?|missing|does not have|do not have)\b[^.]{0,24}\btermination date\b/i.test(text);
  const conditionalDateStep = /\bif\b[^.]{0,160}\b(?:already has|adds|add)\b[^.]{0,40}\btermination date\b/i.test(text);
  if (statesMissingDate && !conditionalDateStep) return true;
  if (new RegExp(`\\b(?:employment|termination|separation) date\\b[^.]{0,24}(?:\\b(?:is|was|of)\\b|[:,])\\s*(?:${EMPLOYMENT_DATE_TEXT})\\b`, 'i').test(asserted)) return true;
  // The same date can arrive as a last day rather than under a date label. The
  // standing last-day question carries no date, so it is not caught here.
  if (new RegExp(`\\blast day\\b(?:\\s+(?:of\\s+employment|worked|you\\s+worked))?[^.]{0,24}(?:\\b(?:is|was|of)\\b|[:,])\\s*(?:${EMPLOYMENT_DATE_TEXT})\\b`, 'i').test(asserted)) return true;
  return false;
}

function exactCurrentEmploymentClaim(text) {
  return sentences(text).some(currentPersonalEmploymentClaim);
}

function notesTreatStatusAsAuthority(notes) {
  if (typeof notes !== 'string' || !notes) return false;
  return sentences(notes).some((sentence) => {
    const statesStatus = exactCurrentEmploymentClaim(sentence) || /\bsystem status\b/i.test(sentence);
    if (!statesStatus) return false;
    if (/\beligib/i.test(sentence) && !/\b(?:not eligibility|does not establish eligibility|not eligible)\b/i.test(sentence)) {
      return true;
    }
    if (/\bverif(?:ied|ies|y)\b/i.test(sentence) && !/\b(?:unverified|not verified)\b/i.test(sentence)) {
      return true;
    }
    return !/\b(?:unverified|not verified|supplied|non-authoritative)\b/i.test(sentence);
  });
}

function qualifySourceMatchedEmploymentNotes(notes, agentResponse) {
  if (typeof notes !== 'string' || !notes) return notes;
  const sourceText = typeof agentResponse === 'string' ? agentResponse :
    agentResponse && typeof agentResponse === 'object' && !Array.isArray(agentResponse) ? JSON.stringify(agentResponse) : '';
  if (!sourceText) return notes;
  // The parser may omit the source's literal field label while copying the
  // same status claim. Permit only that narrow wording change; the entire
  // remaining sentence must still appear in the generated response.
  const sourceWithoutFieldLabel = sourceText.replace(
    new RegExp(`\\bemployment_status\\s+(?=(?:${EMPLOYMENT_STATUS_WORD})\\b)`, 'gi'), '');
  let qualified = notes;
  for (const sentence of sentences(notes)) {
    if (!notesTreatStatusAsAuthority(sentence) ||
        /\b(?:eligib\w*|verified|confirm\w*|establish\w*)\b/i.test(sentence) ||
        (!sourceText.includes(sentence) && !sourceWithoutFieldLabel.includes(sentence))) continue;
    qualified = qualified.replace(sentence,
      'Unverified claim from supplied generated response, not canonical evidence: ' + sentence);
  }
  return qualified;
}

function retainGeneratedProcedure(notes, agentResponse) {
  let generated = agentResponse;
  if (typeof generated === 'string') {
    try { generated = JSON.parse(generated); } catch { return notes; }
  }
  if (!generated || typeof generated !== 'object' || Array.isArray(generated) ||
      typeof generated.internal_notes !== 'string') return notes;
  const lines = generated.internal_notes.split(/\r?\n/).map(line => line.trim());
  const bounded = (line, pattern) => line && line.length <= 500 &&
    pattern.test(line) && !/\beligib\w*\b|\$\s*\d/i.test(line) &&
    !notesTreatStatusAsAuthority(line);
  const manual = lines.find(line => bounded(line, /^2\. .*sponsor payroll access.*termination date.*FUA Admin and American Trust/i));
  const escalation = lines.find(line => bounded(line, /^3\. .*termination date.*plan OPS.*ISSUE.*RM or Sponsor/i));
  const details = [];
  if (manual && !(notes.includes('FUA Admin') && notes.includes('American Trust'))) details.push(manual);
  if (escalation && !(notes.includes('plan OPS') && notes.includes('ISSUE') && notes.includes('RM or Sponsor'))) details.push(escalation);
  if (!details.length) return notes;
  return [notes,
    'Generated source procedural detail for PA verification (not independent participant evidence):\n' + details.join('\n')
  ].filter(Boolean).join('\n\n');
}

function acceptGuardedParserOutput(raw) {
  const original = String(raw ?? '');
  const guarded = guardParserOutput(original);
  if (guarded.status === 'unsafe' && guarded.reason === 'malformed_json') {
    return JSON.parse(original);
  }
  if (guarded.status === 'transformed' || guarded.status === 'unchanged') {
    return JSON.parse(guarded.output);
  }
  const err = new Error(PUBLIC_REJECT);
  err.guardStatus = guarded.status;
  err.guardReason = guarded.reason;
  throw err;
}

const source=$('PA Final Evidence').first().json;
// PRIVATE unpublished class-fix insertion: inlined guard+adapter then frozen e1 tail.
const parsed=acceptGuardedParserOutput(String($input.first().json.output));
const keys=['ticketId','participant_reply','set_stage_solved','stage_reason','internal_notes'];
if(!parsed || typeof parsed!=='object' || Array.isArray(parsed) ||
 Object.keys(parsed).length!==keys.length || keys.some(k=>!Object.hasOwn(parsed,k)) ||
 parsed.ticketId!==source.ticketId || typeof parsed.participant_reply!=='string' ||
 typeof parsed.stage_reason!=='string' || typeof parsed.set_stage_solved!=='boolean' ||
 !(parsed.internal_notes===null || typeof parsed.internal_notes==='string'))
 throw new Error('Invalid PA final draft contract; no ticket write permitted');
const evidence=source.canonical_evidence;
if(!evidence || evidence.publication_authorized!==false || evidence.human_review_required!==true)
 throw new Error('Missing independent canonical evidence decision');
const qualifiedNotes=retainGeneratedProcedure(
  qualifySourceMatchedEmploymentNotes(parsed.internal_notes,source.agentResponse),source.agentResponse);
if(exactCurrentEmploymentClaim(parsed.participant_reply)||exactCurrentEmploymentClaim(parsed.stage_reason)||notesTreatStatusAsAuthority(qualifiedNotes))
 throw new Error(EMPLOYMENT_REJECT);
const displayPlanFacts=(facts=>{
 if(facts==null) return null;
 if(typeof facts!=='object'||Array.isArray(facts)) return facts;
 const projected={};
 for(const key of Object.keys(facts)){
  if(key==='plan_id') continue;
  projected[key]=facts[key];
 }
 return projected;
})(evidence.verified_plan_facts);
const provenance=JSON.stringify({evidence_status:evidence.evidence_status,reason_codes:evidence.reason_codes,
 execution_reference:evidence.execution_reference??null,verified_participant_facts:evidence.verified_participant_facts,
 verified_plan_facts:displayPlanFacts});
const internal=[qualifiedNotes,'Independent backend evidence: '+provenance].filter(Boolean).join('\n\n');
const doubleBreaks=str=>str.replace(/\n/g,'\n\u3164\n\n');
return [{json:{ticketId:source.ticketId,participant_reply:doubleBreaks(parsed.participant_reply),
 set_stage_solved:false,stage_reason:'Advisor review is required. '+parsed.stage_reason,
 internal_notes:doubleBreaks(internal),model_recommended_solved:parsed.set_stage_solved,
 canonical_evidence:evidence,verified_participant_facts:evidence.verified_participant_facts,
 verified_plan_facts:evidence.verified_plan_facts??null,
 human_review_required:true,participant_reply_safe:false,publication_authorized:false,
 verification_reason:evidence.evidence_status==='matched'?'Exact job and conversation matched; generated prose still requires advisor review.':
 'Canonical evidence is incomplete or unavailable; generated prose is not factual authority.'}}];
