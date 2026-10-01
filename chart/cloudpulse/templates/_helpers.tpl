{{/*
Expand the name of the chart.
*/}}
{{- define "cloudpulse.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name, truncated to the 63 character DNS label limit.
*/}}
{{- define "cloudpulse.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version, as used by the helm.sh/chart label.
*/}}
{{- define "cloudpulse.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Selector labels. These must stay stable across upgrades: they are part of an
immutable Deployment selector, so changing them replaces every pod.
*/}}
{{- define "cloudpulse.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cloudpulse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Common labels applied to every object in this chart.
*/}}
{{- define "cloudpulse.labels" -}}
helm.sh/chart: {{ include "cloudpulse.chart" . }}
{{ include "cloudpulse.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: cloudpulse
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Name of the ServiceAccount the pods run as.
*/}}
{{- define "cloudpulse.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "cloudpulse.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding DATABASE_URL, SECRET_KEY and notification
credentials. Fails the render when neither a generated nor an existing
Secret can be resolved, rather than silently deploying a pod that cannot
reach its database.
*/}}
{{- define "cloudpulse.secretName" -}}
{{- if .Values.config.createSecret }}
{{- include "cloudpulse.fullname" . }}
{{- else }}
{{- required "config.existingSecret is required when config.createSecret is false" .Values.config.existingSecret }}
{{- end }}
{{- end }}

{{/*
Container image reference, defaulting the tag to the chart appVersion.
*/}}
{{- define "cloudpulse.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{/*
Keys under `config` that are credentials and therefore belong in a Secret
rather than a ConfigMap.

This is an allow-list of secrets, not a denylist of everything else. A
denylist silently leaks: any new setting a future version adds would default to
"not secret" and land in a ConfigMap, which is readable by anything that can
read pods, is not encrypted at rest by default, and is printed in plaintext by
`helm get manifest`. Everything not named here is treated as non-sensitive.

`config.extra`, `config.extraConfigMap` and `config.extraSecret` are structural
keys of the values file, not application settings, and are never rendered here.
*/}}
{{- define "cloudpulse.secretKeys" -}}
{{- list
      "SECRET_KEY"
      "DATABASE_URL"
      "NOTIFY_WEBHOOK_URL"
      "NOTIFY_WEBHOOK_SECRET"
      "NOTIFY_SMTP_USER"
      "NOTIFY_SMTP_PASSWORD"
-}}
{{- end }}

{{/*
Structural keys of the `config` map. These describe how to render, not what to
configure, so they must never be emitted as environment variables.
*/}}
{{- define "cloudpulse.structuralKeys" -}}
{{- list
      "createSecret"
      "existingSecret"
      "extra"
      "extraConfigMap"
      "extraSecret"
-}}
{{- end }}

{{/*
Look up $key in `.Values.config`. Returns "" when the key is absent, so callers
can use it directly as a condition.
*/}}
{{- define "cloudpulse.hasConfigKey" -}}
{{- if hasKey .Values.config (index . 0) -}}yes{{- end -}}
{{- end }}
