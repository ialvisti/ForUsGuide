const parsed = JSON.parse($input.first().json.output);


// Duplicar saltos de línea para que DevRev renderice línea vacía entre párrafos
const doubleLineBreaks = (str) => 
  typeof str === 'string' ? str.replace(/\n/g, '\n\u3164\n\n') : str;


return [{
  json: {
    ticketId: parsed.ticketId,
    participant_reply: doubleLineBreaks(parsed.participant_reply),
    set_stage_solved: parsed.set_stage_solved,
    stage_reason: parsed.stage_reason,
    internal_notes: doubleLineBreaks(parsed.internal_notes)
  }
}];