// Get the amount of matches and Company name
const participantsFound =  Number($input.first().json.result.data.count)
const aiAgentOutput = $('Relevant information found?1').first().json.output.company_name
var matchStatus = ""


// Perform Validations
if (participantsFound === 1) {


  matchStatus = "Match Found";
  
}else if (participantsFound === 1 && aiAgentOutput != "not found") {


  matchStatus = "AI Needed";
  
}else{


  matchStatus = "No Match Found";
  
}




return{


  json:{


    "matchStatus": matchStatus
    
  }
};