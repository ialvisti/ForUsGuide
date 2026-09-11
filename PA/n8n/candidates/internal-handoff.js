// Replacement for FUG / Stop Unsafe Terminal Result. Its output belongs only
// to a new internal-comment terminal; never connect it to enrichment tags.
const raw = $input.first().json;
const data = Array.isArray(raw) ? raw[0] : raw;
if (data?.state !== 'succeeded' || data.next_action !== 'human_review' || data.metadata?.fallback === true) {
  throw new Error('Ticket job is not a successful human-review result');
}
const fields = $('Get fields1').first().json;
const inquiries = [data.primary, ...(data.related || [])].filter(Boolean).map((iq, index) => {
  const gr = iq.generate_response;
  const kq = iq.knowledge_answer;
  const metadata = gr?.metadata || kq?.metadata || {};
  return {inquiry_index:index, topic:iq.topic, outcome:gr?.response?.outcome,
    data_gaps:gr?.response?.data_gaps, coverage_gaps:gr?.coverage_gaps || kq?.coverage_gaps,
    escalation:gr?.response?.escalation,
    identity_resolution_status:metadata.identity_resolution_status,
    response_source_reason:metadata.response_source_reason,
    incomplete_question_count:metadata.incomplete_question_count};
});
return {json: {
  ticketId:fields.ticketId,
  handoff_only:true,
  visibility:'internal',
  body:JSON.stringify({type:'pa_handoff',ticket_job_id:data.ticket_job_id,
    human_review_required:true,participant_reply_safe:false,next_action:'human_review',inquiries}),
}};
