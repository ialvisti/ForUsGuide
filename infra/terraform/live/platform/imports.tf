# Trigger legacy real: el import impide crear un segundo trigger y permite
# neutralizar c212... in-place conservando su identidad auditada.
import {
  for_each = var.enable_legacy_trigger_neutralization ? {
    legacy = "projects/rag-kb-system/locations/global/triggers/c2126528-7cd3-4063-9214-5eb82e9f76a6"
  } : {}

  to = google_cloudbuild_trigger.main_canonical[each.key]
  id = each.value
}

# G1C prepare importa el grant project-wide existente antes de añadir el
# conditional. En enforce desaparecen import+resource legacy en el mismo plan.
import {
  for_each = (
    var.firestore_scope_migration.enabled &&
    var.firestore_scope_migration.phase == "prepare" &&
    var.firestore_scope_migration.import_legacy
    ) ? {
    legacy = "${var.project_id} roles/datastore.user serviceAccount:kb-rag-runner@${var.project_id}.iam.gserviceaccount.com"
  } : {}

  to = google_project_iam_member.kb_rag_runner_firestore_legacy[each.key]
  id = each.value
}
