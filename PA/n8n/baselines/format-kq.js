function escapeJSONStringForHTTP(obj) {
  const compact = JSON.stringify(obj);
  const escaped = compact
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"');
  return escaped;
}


const input = $input.first().json;
const getFields = $('Get fields1').first().json;


const firstContact = getFields.caseData?.ticketData?.firstContact;


const payload = {
  answer: input.answer,
  key_points: input.key_points,
  confidence_note: input.confidence_note,
  metadata: {
    unique_articles: input.metadata?.unique_articles,
    relevant_articles: input.metadata?.relevant_articles,
    model: input.metadata?.model,
  },
  responseSource: "Knowledge-Question",
};


const bodyPayload = `\`\`\`\\n\\n${escapeJSONStringForHTTP(payload)}\\n\\n`;


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