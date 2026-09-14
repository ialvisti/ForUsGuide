// JSON body expression for Record PA Human Review. Preserve the structured
// note as a string; the HTTP node must receive one valid JSON serialization.
return JSON.stringify({
  object: $json.ticketId,
  body: $json.body,
  visibility: "internal",
  type: "timeline_comment"
});
