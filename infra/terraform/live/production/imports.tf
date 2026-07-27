# Bloques import de producción (plan Tarea 10 Paso 7). NO se ejecutan hasta
# G6B; el primer plan importa in-place y preserva la baseline disabled. Los
# bindings roles/datastore.user de kb-rag-runner (project-wide y scoped) viven
# SÓLO en live/platform (G1C), nunca aquí: ningún member/resource en dos states.

# Servicio producer existente. El import sólo es ejecutable junto con el
# inventario obligatorio de producer_core_env/secret_version_refs y el rollback
# anchor producer_baseline_revision; así no puede adoptar un desired state
# parcial que borre configuración core o mande tráfico implícito a latest.
import {
  to = module.production.google_cloud_run_v2_service.producer[0]
  id = "projects/rag-kb-system/locations/us-central1/services/kb-rag-system"
}

# La database `(default)` y los secret containers se importan en platform.
# Production sólo crea recursos hijos y accessor IAM sobre IDs aprobados.

# Policy legacy inventariada read-only el 2026-07-20. Se adopta antes de
# deshabilitar/renombrar para que Terraform neutralice el recurso existente y
# jamás cree un duplicado con el mismo significado ambiguo.
import {
  to = module.production.google_monitoring_alert_policy.legacy_high_error_rate[0]
  id = "projects/rag-kb-system/alertPolicies/15030298849808887870"
}
