# Bloques import de producción (plan Tarea 10 Paso 7). NO se ejecutan hasta
# G6B; el primer plan importa in-place y preserva la baseline disabled. Los
# bindings roles/datastore.user de kb-rag-runner (project-wide y scoped) viven
# SÓLO en live/platform (G1C), nunca aquí: ningún member/resource en dos states.

# Servicio producer existente (kb-rag-system) con su revisión segura actual.
import {
  to = module.production.google_cloud_run_v2_service.producer[0]
  id = "projects/rag-kb-system/locations/us-central1/services/kb-rag-system"
}

# La cola, worker, reconciler, base (default) y SAs nuevas NO se importan:
# se crean. El binding invoker existente (kb-rag-client) se importa para no
# recrearlo.
import {
  to = module.production.google_cloud_run_v2_service_iam_member.producer_preserved_invokers["serviceAccount:kb-rag-client@rag-kb-system.iam.gserviceaccount.com"]
  id = "projects/rag-kb-system/locations/us-central1/services/kb-rag-system roles/run.invoker serviceAccount:kb-rag-client@rag-kb-system.iam.gserviceaccount.com"
}
