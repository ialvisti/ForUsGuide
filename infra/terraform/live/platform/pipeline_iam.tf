# Acceso funcional de CI/CD. Los builders no tienen deploy/state; plan es
# read-only + lock propio; apply recibe sólo servicios administrados por su
# root y el bucket de state de ese mismo root.

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  # Builders separados aunque el repo `kb-rag` ya existente sea compartido.
  # Tags inmutables + manifests por digest impiden sustituir un artefacto ya
  # atestado; ninguna de estas identidades tiene deploy/state.
  image_builders = merge(
    {
      runtime = {
        email = google_service_account.ci.email
      }
      release_controller = {
        email = google_service_account.controller_builder.email
      }
    },
    var.cicd_bootstrap.enabled ? {
      e2e = {
        email = google_service_account.e2e_image[0].email
      }
    } : {},
  )

  builder_evidence_prefixes = merge(
    {
      runtime = {
        email  = google_service_account.ci.email
        prefix = "runtime/"
      }
    },
    var.cicd_bootstrap.enabled ? {
      e2e = {
        email  = google_service_account.e2e_image[0].email
        prefix = "e2e-images/"
      }
      runtime-attest = {
        email  = google_service_account.runtime_attest[0].email
        prefix = "runtime/"
      }
    } : {},
  )

  # Cada gate usa una identidad exclusiva. Estas SAs sólo se conectan a los
  # grants funcionales mínimos de Cloud Build que aparecen abajo; nunca se
  # incorporan a builders, state_pipelines ni evidence writers/readers.
  gate_receipt_sas = var.cicd_bootstrap.enabled ? {
    for gate, account in google_service_account.gate_receipt :
    gate => account.email
  } : {}

  gate_receipt_approvers = var.cicd_bootstrap.enabled ? toset(flatten([
    for accounts in values(var.gate_approver_accounts) : tolist(accounts)
  ])) : toset([])

  # Builders y consumidores de receipts inspeccionan builds/triggers exactos
  # para ligar provenance/autorización a BUILD_ID. Nunca necesitan listar,
  # crear ni cancelar builds.
  provenance_builders = merge(
    {
      runtime = {
        email = google_service_account.ci.email
      }
    },
    var.cicd_bootstrap.enabled ? {
      e2e = {
        email = google_service_account.e2e_image[0].email
      }
      runtime-attest = {
        email = google_service_account.runtime_attest[0].email
      }
      evidence-manifest = {
        email = google_service_account.evidence_manifest[0].email
      }
      staging-attest = {
        email = google_service_account.staging_attest[0].email
      }
    } : {},
    {
      for gate, email in local.gate_receipt_sas :
      "gate-${gate}" => { email = email }
    },
  )

  state_pipelines = var.cicd_bootstrap.enabled ? {
    platform = {
      bucket      = "rag-kb-system-tfstate-platform-900340137010"
      plan_email  = google_service_account.plan_platform[0].email
      apply_email = google_service_account.apply_platform[0].email
    }
    staging = {
      bucket      = "rag-kb-system-tfstate-staging-900340137010"
      plan_email  = google_service_account.plan_staging[0].email
      apply_email = google_service_account.apply_staging[0].email
    }
    production = {
      bucket      = "rag-kb-system-tfstate-production-900340137010"
      plan_email  = google_service_account.plan_production[0].email
      apply_email = google_service_account.apply_production[0].email
    }
  } : {}

  privileged_pipeline_sas = var.cicd_bootstrap.enabled ? {
    platform-plan     = google_service_account.plan_platform[0].email
    platform-apply    = google_service_account.apply_platform[0].email
    staging-plan      = google_service_account.plan_staging[0].email
    staging-apply     = google_service_account.apply_staging[0].email
    production-plan   = google_service_account.plan_production[0].email
    production-apply  = google_service_account.apply_production[0].email
    staging-attest    = google_service_account.staging_attest[0].email
    staging-observer  = google_service_account.staging_observer[0].email
    evidence-manifest = google_service_account.evidence_manifest[0].email
    test-only         = google_service_account.test_only[0].email
    e2e-image         = google_service_account.e2e_image[0].email
    runtime-attest    = google_service_account.runtime_attest[0].email
  } : {}

  controller_runtime_sas = var.cicd_bootstrap.enabled ? merge(
    local.privileged_pipeline_sas,
    { runtime-image = google_service_account.ci.email },
    {
      for gate, email in local.gate_receipt_sas :
      "gate-${gate}" => email
    },
  ) : {}

  # Incluye las dos identidades bootstrap no privilegiadas y todas las SAs de
  # trigger. Nunca incluye Compute/default Cloud Build.
  build_execution_sas = merge(
    {
      ci                  = google_service_account.ci.email
      controller-verifier = google_service_account.controller_verifier.email
      controller-builder  = google_service_account.controller_builder.email
    },
    local.privileged_pipeline_sas,
    {
      for gate, email in local.gate_receipt_sas :
      "gate-${gate}" => email
    },
  )

  # El apply de platform crea/actualiza cada trigger y su controller, por lo
  # que Cloud Build exige iam.serviceAccounts.actAs sobre la identidad exacta
  # configurada en cada uno. Se incluyen también los dos schedulers que ya son
  # propiedad del root platform. Las identidades bootstrap del verificador y
  # del publicador se excluyen deliberadamente: platform-apply no debe poder
  # asumirlas y no existe un trigger que las seleccione. Nunca se concede
  # Service Account User a nivel de proyecto ni sobre una SA fuera de esta
  # matriz declarativa.
  platform_apply_actas_sas = var.cicd_bootstrap.enabled ? merge(
    {
      scheduler-staging    = google_service_account.runtime["ticket-scheduler-stg"].name
      scheduler-production = google_service_account.runtime["ticket-scheduler-prod"].name
    },
    {
      for purpose, email in local.controller_runtime_sas :
      "build-${purpose}" => "projects/${var.project_id}/serviceAccounts/${email}"
    },
  ) : {}

  plan_pipelines = var.cicd_bootstrap.enabled ? {
    platform = {
      email = google_service_account.plan_platform[0].email
      role  = google_project_iam_custom_role.platform_plan_reader[0].id
    }
    staging = {
      email = google_service_account.plan_staging[0].email
      role  = google_project_iam_custom_role.environment_plan_reader[0].id
    }
    production = {
      email = google_service_account.plan_production[0].email
      role  = google_project_iam_custom_role.environment_plan_reader[0].id
    }
  } : {}

  # Logging/Monitoring no ofrecen todos sus recursos como IAM children. Este
  # residual excluye explícitamente Security Admin y Secret Manager Admin;
  # project IAM vive en platform y secrets usan un custom role por ID exacto.
  environment_apply_residual_project_roles = toset([
    "roles/logging.configWriter",
    "roles/monitoring.editor",
    "roles/serviceusage.serviceUsageConsumer",
  ])

  apply_pipeline_roles = var.cicd_bootstrap.enabled ? {
    platform = {
      email = google_service_account.apply_platform[0].email
      roles = toset([
        "roles/serviceusage.serviceUsageAdmin",
        "roles/artifactregistry.admin",
        "roles/iam.serviceAccountAdmin",
        "roles/iam.roleAdmin",
        "roles/cloudbuild.builds.editor",
      ])
    }
    staging = {
      email = google_service_account.apply_staging[0].email
      roles = local.environment_apply_residual_project_roles
    }
    production = {
      email = google_service_account.apply_production[0].email
      roles = local.environment_apply_residual_project_roles
    }
  } : {}

  environment_apply_boundaries = var.cicd_bootstrap.enabled ? {
    staging = {
      email    = google_service_account.apply_staging[0].email
      database = "ticket-staging"
    }
    production = {
      email    = google_service_account.apply_production[0].email
      database = "(default)"
    }
  } : {}

  environment_plan_bucket_readers = var.cicd_bootstrap.enabled ? {
    staging    = google_service_account.plan_staging[0].email
    production = google_service_account.plan_production[0].email
  } : {}

  environment_run_creators = var.cicd_bootstrap.enabled ? {
    for environment, boundary in local.environment_apply_boundaries :
    environment => boundary
    if var.environment_handoff_phase[environment] == "bootstrap"
  } : {}

  environment_apply_secret_grants = var.cicd_bootstrap.enabled ? merge([
    for environment, boundary in local.environment_apply_boundaries : {
      for secret_id in var.environment_secret_ids[environment] :
      "${environment}-${secret_id}" => {
        environment = environment
        email       = boundary.email
        secret_id   = secret_id
      }
    } if var.environment_container_phase[environment] == "managed"
  ]...) : {}

  apply_functional_grants = flatten([
    for pipeline, config in local.apply_pipeline_roles : [
      for role in config.roles : {
        pipeline = pipeline
        email    = config.email
        role     = role
      }
    ]
  ])

  # actAs se concede sobre SAs runtime concretas, nunca a nivel proyecto.
  environment_apply_runtime_sas = var.cicd_bootstrap.enabled ? merge(
    {
      for account_id in [
        "ticket-producer-stg",
        "ticket-worker-stg",
        "ticket-reconciler-stg",
        "ticket-e2e-stg",
        ] : "staging-${account_id}" => {
        account_id  = account_id
        apply_email = google_service_account.apply_staging[0].email
      }
    },
    {
      for account_id in [
        "ticket-producer-prod",
        "ticket-worker-prod",
        "ticket-reconciler-prod",
        ] : "production-${account_id}" => {
        account_id  = account_id
        apply_email = google_service_account.apply_production[0].email
      }
    },
  ) : {}
}

# --- Read-only refresh permissions -----------------------------------------

resource "google_project_iam_custom_role" "platform_plan_reader" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformPlanRead"
  title       = "Ticket Terraform platform plan reader"
  description = "Metadata/IAM refresh del root platform; sin mutación ni object data."
  permissions = [
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.getIamPolicy",
    "cloudbuild.builds.get",
    "cloudbuild.builds.list",
    "cloudscheduler.jobs.get",
    "cloudtasks.queues.get",
    "cloudtasks.queues.getIamPolicy",
    "datastore.databases.getMetadata",
    "iam.roles.get",
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.getIamPolicy",
    "resourcemanager.projects.get",
    "resourcemanager.projects.getIamPolicy",
    "run.jobs.get",
    "run.jobs.getIamPolicy",
    "run.services.get",
    "run.services.getIamPolicy",
    "secretmanager.secrets.get",
    "secretmanager.secrets.getIamPolicy",
    "serviceusage.services.get",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
  ]
}

resource "google_project_iam_custom_role" "environment_plan_reader" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfEnvironmentPlanRead"
  title       = "Ticket Terraform environment plan reader"
  description = "Config refresh staging/production; explícitamente sin lectura de entidades Firestore."
  permissions = [
    "cloudbuild.builds.get",
    "datastore.databases.getMetadata",
    "datastore.operations.get",
    "datastore.operations.list",
    "datastore.schemas.get",
    "datastore.schemas.list",
    "logging.logMetrics.get",
    "monitoring.alertPolicies.get",
    "monitoring.dashboards.get",
    "resourcemanager.projects.get",
    "run.jobs.get",
    "run.operations.get",
    "run.operations.list",
    "run.services.get",
    "secretmanager.secrets.get",
    "secretmanager.secrets.getIamPolicy",
  ]
}

resource "google_project_iam_custom_role" "build_provenance_reader" {
  project     = var.project_id
  role_id     = "ticketBuildProvenanceRead"
  title       = "Ticket build provenance reader"
  description = "Get-only de builds/triggers exactos para provenance y receipts."
  permissions = ["cloudbuild.builds.get"]
}

# Los comandos de observación sólo describen el service/job de staging. Un
# rol custom evita el alcance adicional (list/revisions) de roles/run.viewer.
resource "google_project_iam_custom_role" "staging_observer_run_reader" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketStagingObserverRunRead"
  title       = "Ticket staging observer Run reader"
  description = "Get-only de servicios/jobs Run para observación gateada."
  permissions = [
    "run.jobs.get",
    "run.services.get",
  ]
}

resource "google_project_iam_member" "plan_functional" {
  for_each = local.plan_pipelines
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "build_provenance_reader" {
  for_each = local.provenance_builders
  project  = var.project_id
  role     = google_project_iam_custom_role.build_provenance_reader.id
  member   = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "staging_observer_run_reader" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.staging_observer_run_reader[0].id
  member  = "serviceAccount:${google_service_account.staging_observer[0].email}"
}

# --- Apply permissions, separated by Terraform root -----------------------

# Platform administers bucket metadata/IAM but never object payloads in every
# bucket (unlike roles/storage.admin). Object access remains per-bucket below.
resource "google_project_iam_custom_role" "platform_storage_admin" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformStorageAdmin"
  title       = "Ticket Terraform platform bucket admin"
  description = "Bucket lifecycle/IAM only; no storage.objects permissions."
  permissions = [
    "resourcemanager.projects.get",
    "storage.buckets.create",
    "storage.buckets.delete",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.list",
    "storage.buckets.setIamPolicy",
    "storage.buckets.update",
  ]
}

# Platform crea/importa las databases sobre el parent project. Este broker no
# contiene ninguna operación datastore.entities.*.
resource "google_project_iam_custom_role" "platform_firestore_database_broker" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformFirestore"
  title       = "Ticket Terraform platform Firestore broker"
  description = "Create/import/update de database containers; nunca entidades."
  permissions = [
    "datastore.databases.create",
    "datastore.databases.getMetadata",
    "datastore.databases.update",
    "datastore.operations.get",
    "datastore.operations.list",
  ]
}

# Terraform gestiona definición/horario; no necesita ejecutar, pausar ni
# forzar jobs manualmente.
resource "google_project_iam_custom_role" "platform_scheduler_broker" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformScheduler"
  title       = "Ticket Terraform platform Scheduler broker"
  description = "CRUD/pausa declarativa de los dos jobs; sin jobs.run."
  permissions = [
    "cloudscheduler.jobs.create",
    "cloudscheduler.jobs.get",
    "cloudscheduler.jobs.pause",
    "cloudscheduler.jobs.resume",
    "cloudscheduler.jobs.update",
    "cloudscheduler.locations.get",
    "cloudscheduler.locations.list",
  ]
}

resource "google_project_iam_custom_role" "platform_queue_broker" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformQueues"
  title       = "Ticket Terraform platform queue broker"
  description = "Create/update/IAM/pause de las dos queues; nunca tasks/purge/resume."
  permissions = [
    "cloudtasks.locations.get",
    "cloudtasks.locations.list",
    "cloudtasks.queues.create",
    "cloudtasks.queues.get",
    "cloudtasks.queues.getIamPolicy",
    "cloudtasks.queues.pause",
    "cloudtasks.queues.setIamPolicy",
    "cloudtasks.queues.update",
  ]
}

# El controller sólo necesita comprobar que una queue pausada está vacía. La
# lectura de task metadata queda separada del broker CRUD y se concede abajo
# directamente sobre cada queue administrada; nunca obtiene tasks.get/run.
resource "google_project_iam_custom_role" "platform_queue_task_inspector" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformQueueTaskInspector"
  title       = "Ticket Terraform queue task inspector"
  description = "List metadata sólo en las queues ticket exactas durante pause preflight."
  permissions = [
    "cloudtasks.tasks.list",
  ]
}

resource "google_project_iam_custom_role" "platform_run_iam_broker" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformRunIam"
  title       = "Ticket Terraform platform Run IAM broker"
  description = "Get/set IAM de Run; el controller limita targets al inventario exacto."
  permissions = [
    "run.jobs.get",
    "run.jobs.getIamPolicy",
    "run.jobs.setIamPolicy",
    "run.services.get",
    "run.services.getIamPolicy",
    "run.services.setIamPolicy",
  ]
}

# Temporal y create-only. Sólo existe durante handoff_phase=bootstrap; tras
# inventariar los recursos se reemplaza por roles/run.developer ligados al
# service/job concreto en runtime_project_iam.tf.
resource "google_project_iam_custom_role" "environment_run_creator" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfEnvironmentRunCreate"
  title       = "Ticket Terraform temporary Run creator"
  description = "Create-only temporal; sin update/delete/IAM/invoke."
  permissions = [
    "run.jobs.create",
    "run.services.create",
  ]
}

# Cada root de entorno gestiona un único binding de objectViewer para su
# producer sobre el bucket RAG preexistente. Terraform necesita leer/escribir
# la policy del bucket, pero nunca listar, leer, crear ni borrar objetos.
resource "google_project_iam_custom_role" "environment_bucket_iam_admin" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfEnvironmentBucketIam"
  title       = "Ticket Terraform environment RAG bucket IAM admin"
  description = "IAM del bucket RAG exacto por entorno; sin acceso a objetos."
  permissions = [
    "resourcemanager.projects.get",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.setIamPolicy",
  ]
}

# Cada plan de entorno refresca el único bucket IAM de su root sin poder mutar
# su policy ni leer/listar objetos.
resource "google_project_iam_custom_role" "environment_bucket_iam_reader" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfEnvironmentBucketRead"
  title       = "Ticket Terraform environment RAG bucket IAM reader"
  description = "Read-only de metadata/IAM del bucket RAG exacto."
  permissions = [
    "resourcemanager.projects.get",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
  ]
}

# Platform es el único broker de project IAM. El controller limita sus planes
# a los bindings runtime declarados; environment apply nunca recibe este role.
resource "google_project_iam_custom_role" "platform_project_iam_broker" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformIamBroker"
  title       = "Ticket Terraform platform IAM broker"
  description = "Project/service-account IAM para el root platform confiable."
  permissions = [
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
    "resourcemanager.projects.get",
    "resourcemanager.projects.getIamPolicy",
    "resourcemanager.projects.setIamPolicy",
  ]
}

resource "google_project_iam_custom_role" "platform_secret_container_broker" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfPlatformSecrets"
  title       = "Ticket Terraform platform secret container broker"
  description = "Create/import/update de containers; nunca versiones o payloads."
  permissions = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.get",
    "secretmanager.secrets.update",
  ]
}

# Metadata/IAM de containers ya creados, sin una sola operación de secret
# versions. Cada binding queda condicionado a un ID aprobado y disjunto.
resource "google_project_iam_custom_role" "environment_secret_container_admin" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTfEnvironmentSecretAdmin"
  title       = "Ticket Terraform environment secret container admin"
  description = "Metadata/container/IAM de secrets exactos; nunca payload/version operations."
  permissions = [
    "secretmanager.secrets.get",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",
    "secretmanager.secrets.update",
  ]
}

resource "google_project_iam_member" "apply_functional" {
  for_each = {
    for grant in local.apply_functional_grants :
    "${grant.pipeline}-${replace(grant.role, "/", "-")}" => grant
  }
  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${each.value.email}"
}

# High-risk CRUD permissions are conditioned to the exact environment resource
# names. This prevents an apply SA from using Cloud Run/Tasks/IAM Admin against
# the sibling environment even outside Terraform.
resource "google_project_iam_member" "environment_run_creator" {
  for_each = local.environment_run_creators
  project  = var.project_id
  role     = google_project_iam_custom_role.environment_run_creator[0].id
  member   = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "environment_apply_index_admin" {
  for_each = {
    for environment, boundary in local.environment_apply_boundaries :
    environment => boundary
    if var.environment_container_phase[environment] == "managed"
  }
  project = var.project_id
  role    = "roles/datastore.indexAdmin"
  member  = "serviceAccount:${each.value.email}"

  condition {
    title       = "${each.key}_database_schema_only"
    description = "Índices/TTL exclusivamente en la database ${each.value.database}."
    expression  = "resource.name.startsWith(\"projects/${var.project_id}/databases/${each.value.database}/\") || resource.name == \"projects/${var.project_id}/databases/${each.value.database}\""
  }
}

resource "google_project_iam_member" "environment_apply_secret_admin" {
  for_each = local.environment_apply_secret_grants
  project  = var.project_id
  role     = google_project_iam_custom_role.environment_secret_container_admin[0].id
  member   = "serviceAccount:${each.value.email}"

  condition {
    title       = "${each.value.environment}_${substr(sha256(each.value.secret_id), 0, 12)}_secret_only"
    description = "Container/IAM sólo del secret aprobado para ${each.value.environment}."
    expression  = "resource.name == \"projects/${data.google_project.current.number}/secrets/${each.value.secret_id}\""
  }
}

# Custom-role IDs son computed hasta apply; mantener bindings separados evita
# que formen claves desconocidas de for_each y hace el plan determinista.
resource "google_project_iam_member" "platform_apply_storage" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.platform_storage_admin[0].id
  member  = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

resource "google_project_iam_member" "platform_apply_iam_broker" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.platform_project_iam_broker[0].id
  member  = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

resource "google_project_iam_member" "platform_apply_firestore_broker" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.platform_firestore_database_broker[0].id
  member  = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

resource "google_project_iam_member" "platform_apply_secret_broker" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.platform_secret_container_broker[0].id
  member  = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

resource "google_project_iam_member" "platform_apply_queue_broker" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.platform_queue_broker[0].id
  member  = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

resource "google_cloud_tasks_queue_iam_member" "platform_apply_queue_task_inspector" {
  for_each = var.cicd_bootstrap.enabled ? local.environment_queues : {}
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.environment[each.key].name
  role     = google_project_iam_custom_role.platform_queue_task_inspector[0].id
  member   = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

resource "google_project_iam_member" "platform_apply_scheduler_broker" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.platform_scheduler_broker[0].id
  member  = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

resource "google_project_iam_member" "platform_apply_run_iam_broker" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.platform_run_iam_broker[0].id
  member  = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

# Apply también necesita refresh/locations/operations durante create/update.
# Reutiliza el reader metadata-only; no incorpora permisos de datos ni CRUD.
resource "google_project_iam_member" "environment_apply_metadata_reader" {
  for_each = local.environment_apply_boundaries
  project  = var.project_id
  role     = google_project_iam_custom_role.environment_plan_reader[0].id
  member   = "serviceAccount:${each.value.email}"
}

resource "google_storage_bucket_iam_member" "staging_apply_rag_bucket_iam" {
  for_each = local.environment_apply_boundaries
  bucket   = "rag-kb-system-kb-articles"
  role     = google_project_iam_custom_role.environment_bucket_iam_admin[0].id
  member   = "serviceAccount:${each.value.email}"
}

resource "google_storage_bucket_iam_member" "staging_plan_rag_bucket_reader" {
  for_each = local.environment_plan_bucket_readers
  bucket   = "rag-kb-system-kb-articles"
  role     = google_project_iam_custom_role.environment_bucket_iam_reader[0].id
  member   = "serviceAccount:${each.value}"
}

resource "google_service_account_iam_member" "environment_apply_actas" {
  for_each           = local.environment_apply_runtime_sas
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value.account_id}@${var.project_id}.iam.gserviceaccount.com"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${each.value.apply_email}"
}

resource "google_service_account_iam_member" "platform_apply_actas_scheduler" {
  for_each           = local.platform_apply_actas_sas
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.apply_platform[0].email}"
}

# --- State and evidence boundaries ----------------------------------------

resource "google_project_iam_custom_role" "terraform_plan_lock" {
  count       = var.cicd_bootstrap.enabled ? 1 : 0
  project     = var.project_id
  role_id     = "ticketTerraformPlanLock"
  title       = "Ticket Terraform plan lock"
  description = "Create/delete exclusivamente el lock del backend; sin state write."
  permissions = [
    "storage.objects.create",
    "storage.objects.delete",
  ]
}

resource "google_storage_bucket_iam_member" "plan_state_viewer" {
  for_each = local.state_pipelines
  bucket   = each.value.bucket
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${each.value.plan_email}"
}

resource "google_storage_bucket_iam_member" "plan_state_lock" {
  for_each = local.state_pipelines
  bucket   = each.value.bucket
  role     = google_project_iam_custom_role.terraform_plan_lock[0].id
  member   = "serviceAccount:${each.value.plan_email}"

  condition {
    title       = "${each.key}_default_workspace_lock"
    description = "Sólo state/default.tflock del backend ${each.key}."
    expression  = "resource.name == \"projects/_/buckets/${each.value.bucket}/objects/state/default.tflock\""
  }
}

resource "google_storage_bucket_iam_member" "apply_state_admin" {
  for_each = local.state_pipelines
  bucket   = each.value.bucket
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${each.value.apply_email}"
}

# Plan crea objetos nuevos; apply sólo lee plan/manifest aprobado.
resource "google_storage_bucket_iam_member" "plan_evidence_writer" {
  for_each = local.state_pipelines
  bucket   = google_storage_bucket.evidence.name
  role     = "roles/storage.objectCreator"
  member   = "serviceAccount:${each.value.plan_email}"

  condition {
    title       = "${each.key}_plan_evidence_only"
    description = "Create-only exclusivamente bajo plans/${each.key}/."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/plans/${each.key}/\")"
  }
}

resource "google_storage_bucket_iam_member" "apply_evidence_reader" {
  for_each = local.state_pipelines
  bucket   = google_storage_bucket.evidence.name
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${each.value.apply_email}"
}

# El controller de platform apply publica el manifest firmado de outputs que
# alimenta staging/production. objectCreator conserva semántica write-once.
resource "google_storage_bucket_iam_member" "platform_apply_evidence_writer" {
  count  = var.cicd_bootstrap.enabled ? 1 : 0
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.apply_platform[0].email}"

  condition {
    title       = "platform_outputs_only"
    description = "Create-only exclusivamente bajo platform-outputs/."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/platform-outputs/\")"
  }
}

# Build/SBOM/scan sólo puede crear objetos; versioning no sustituye esta
# prohibición explícita de overwrite/delete.
resource "google_storage_bucket_iam_member" "builder_evidence_writer" {
  for_each = local.builder_evidence_prefixes
  bucket   = google_storage_bucket.evidence.name
  role     = "roles/storage.objectCreator"
  member   = "serviceAccount:${each.value.email}"

  condition {
    title       = "${each.key}_image_evidence_only"
    description = "Create-only exclusivamente bajo ${each.value.prefix}."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/${each.value.prefix}\")"
  }
}

# El runtime E2E publica únicamente los envelopes E2E/differential ya
# sanitizados. objectCreator + ifGenerationMatch=0 impide leer, sobrescribir o
# borrar evidencia previa.
resource "google_storage_bucket_iam_member" "e2e_runtime_evidence_writer" {
  count  = var.cicd_bootstrap.enabled ? 1 : 0
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.runtime["ticket-e2e-stg"].email}"

  condition {
    title       = "staging_e2e_results_only"
    description = "Create-only de resultados live E2E/differential; sin prefijos de release."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/handle-ticket/e2e/\")"
  }
}

resource "google_storage_bucket_iam_member" "aux_evidence_writer" {
  for_each = var.cicd_bootstrap.enabled ? {
    staging-attest = {
      email  = google_service_account.staging_attest[0].email
      prefix = "promotions/"
    }
    evidence-manifest = {
      email  = google_service_account.evidence_manifest[0].email
      prefix = "evidence/"
    }
  } : {}
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${each.value.email}"

  condition {
    title       = "${each.key}_output_only"
    description = "Create-only exclusivamente bajo ${each.value.prefix}."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/${each.value.prefix}\")"
  }
}

resource "google_storage_bucket_iam_member" "aux_evidence_reader" {
  for_each = var.cicd_bootstrap.enabled ? {
    staging-plan      = google_service_account.plan_staging[0].email
    production-plan   = google_service_account.plan_production[0].email
    staging-attest    = google_service_account.staging_attest[0].email
    evidence-manifest = google_service_account.evidence_manifest[0].email
    test-only         = google_service_account.test_only[0].email
  } : {}
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${each.value}"
}

# Observaciones y rollback escriben en namespaces distintos y write-once. La
# lectura se limita a la evidencia E2E que rollback debe fijar por generation.
resource "google_storage_bucket_iam_member" "staging_observer_evidence_writer" {
  for_each = var.cicd_bootstrap.enabled ? toset([
    "staging-observations/",
    "rollback-observations/",
  ]) : []
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.staging_observer[0].email}"

  condition {
    title       = "${trimsuffix(each.value, "/")}_only"
    description = "Create-only exclusivamente bajo ${each.value}."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/${each.value}\")"
  }
}

resource "google_storage_bucket_iam_member" "staging_observer_e2e_reader" {
  count  = var.cicd_bootstrap.enabled ? 1 : 0
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.staging_observer[0].email}"

  condition {
    title       = "staging_observer_e2e_only"
    description = "Read-only exclusivamente bajo handle-ticket/e2e/."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/handle-ticket/e2e/\")"
  }
}

# --- Artifact build/read/scan ---------------------------------------------

resource "google_artifact_registry_repository_iam_member" "image_writer" {
  for_each   = local.image_builders
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "image_scanner" {
  for_each = local.image_builders
  project  = var.project_id
  role     = "roles/ondemandscanning.admin"
  member   = "serviceAccount:${each.value.email}"
}

# El controller ejecutado por los triggers está fijado al image name y digest
# exactos dentro del repo importado.
resource "google_artifact_registry_repository_iam_member" "controller_reader" {
  for_each   = local.controller_runtime_sas
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"

  lifecycle {
    precondition {
      condition = startswith(
        var.cicd_bootstrap.release_controller_image_digest,
        "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/release-controller@sha256:",
      )
      error_message = "el release-controller debe usar kb-rag/release-controller por digest en el proyecto/región declarados."
    }
  }
}

# test-only revalida/pullea el digest runtime, pero nunca publica en el repo.
resource "google_artifact_registry_repository_iam_member" "test_only_runtime_reader" {
  count      = var.cicd_bootstrap.enabled ? 1 : 0
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.test_only[0].email}"
}

# Cloud Run exige que el principal que configura el servicio pueda resolver la
# imagen. Artifact Registry sólo puede acotar reader al repo compartido; el
# grant no permite a apply publicar, retaggear ni borrar ningún paquete.
resource "google_artifact_registry_repository_iam_member" "environment_apply_runtime_reader" {
  for_each = var.cicd_bootstrap.enabled ? {
    staging    = google_service_account.apply_staging[0].email
    production = google_service_account.apply_production[0].email
  } : {}
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"
}

# --- Cloud Build execution and release ownership --------------------------

# Según el contrato de custom Cloud Build SA, el service agent sólo puede
# acuñar credenciales cortas de estas SAs explícitas. No se usa Compute default.
resource "google_service_account_iam_member" "cloud_build_executes_as" {
  for_each           = local.build_execution_sas
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
}

resource "google_service_account_iam_member" "production_release_group" {
  count              = var.cicd_bootstrap.enabled ? 1 : 0
  service_account_id = google_service_account.apply_production[0].name
  role               = "roles/iam.serviceAccountUser"
  member             = "group:${var.production_release_group_email}"
}

resource "google_project_iam_member" "production_release_approver" {
  count   = var.cicd_bootstrap.enabled ? 1 : 0
  project = var.project_id
  role    = "roles/cloudbuild.builds.approver"
  member  = "group:${var.production_release_group_email}"
}

# Cloud Build IAM es project-wide, pero cada approval sólo obtiene autoridad si
# el controller lo observa en el trigger/rol exacto y completa un quorum de
# principals distintos. Estas cuentas no reciben run/create ni actAs.
resource "google_project_iam_member" "gate_receipt_approver" {
  for_each = local.gate_receipt_approvers
  project  = var.project_id
  role     = "roles/cloudbuild.builds.approver"
  member   = "user:${each.value}"
}

# Custom build SAs necesitan emitir logs. Ninguna recibe roles primitivos,
# Compute default, Run Admin ni acceso global a buckets/state.
resource "google_project_iam_member" "pipeline_logs" {
  for_each = local.build_execution_sas
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = "serviceAccount:${each.value}"
}

# Cloud Build almacena el source tarball del submit manual en este bucket
# legacy. El verificador sólo necesita leer ese input; no puede escribir,
# publicar imágenes, desplegar, leer state ni acceder al evidence bucket.
resource "google_storage_bucket_iam_member" "controller_verifier_source_reader" {
  bucket = "${var.project_id}_cloudbuild"
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.controller_verifier.email}"
}
