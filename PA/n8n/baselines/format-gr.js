function escapeJSONStringForHTTP(obj) {
  const compact = JSON.stringify(obj);
  const escaped = compact
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"');
  return escaped;
}


// ---- inputs ------------------------------------------------------------
const raw = $input.first().json;
// /handle-ticket devuelve un array (o un objeto en la ruta inline). Normalizamos.
const data = Array.isArray(raw) ? raw[0] : raw;


const getFields = $('Get fields1').first().json;
const firstContact =
  getFields.caseData?.ticketData?.firstContact ??
  getFields.ticketData?.firstContact ??
  getFields.firstContact;


// ---- quedarse SOLO con lo que el LLM redactor necesita ------------------
function cleanInquiry(iq) {
  if (!iq) return null;


  const out = {
    inquiry: iq.inquiry,
    topic: iq.topic,
    route: iq.route,
  };


  // Ruta generate_response: contenido accionable para el participante
  const r = iq.generate_response?.response;
  if (r) {
    out.outcome = r.outcome;
    out.response_to_participant = r.response_to_participant; // opening, key_points, steps, warnings
    out.questions_to_ask = r.questions_to_ask;
    out.escalation = r.escalation;
  }


  // Ruta knowledge_question
  if (iq.knowledge_answer) {
    out.knowledge_answer = {
      answer: iq.knowledge_answer.answer,
      key_points: iq.knowledge_answer.key_points,
    };
  }


  // Ruta needs_more_info
  if (iq.needs_more_info_message) {
    out.needs_more_info_message = iq.needs_more_info_message;
  }


  return out;
}


// primary + related (soporta tickets multi-inquiry, p.ej. financiero + account_access)
const inquiries = [data.primary, ...(data.related || [])]
  .filter(Boolean)
  .map(cleanInquiry);


const payload = {
  responseSource: "Generate-Response",
  total_inquiries_in_ticket: data.total_inquiries_in_ticket,
  inquiries,
};


const bodyPayload = `\`\`\`\\n\\n${escapeJSONStringForHTTP(payload)}\\n\\n`;


// ---- tags (sin cambios) ------------------------------------------------
let tags = [
  { id: "don:core:dvrv-us-1:devo/1is7v8y722:tag/2910" }
];


if (firstContact === false || firstContact === 'false') {
  tags.push({ id: "don:core:dvrv-us-1:devo/1is7v8y722:tag/2887" });
} else {
  tags.push({ id: "don:core:dvrv-us-1:devo/1is7v8y722:tag/2886" });
}


const tagString = tags.map(t => JSON.stringify(t)).join(",\n  ");


return {
  json: {
    ticketId: getFields.ticketId,
    body: bodyPayload,
    part: "CAPL-1",
    tag: tagString,
  }
};