{{- define "stroom-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "stroom-mcp.fullname" -}}
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

{{- define "stroom-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "stroom-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "stroom-mcp.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "stroom-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "stroom-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "stroom-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* The Secret this chart creates for values given inline (request-state keys). */}}
{{- define "stroom-mcp.secretName" -}}
{{- printf "%s-secrets" (include "stroom-mcp.fullname" .) }}
{{- end }}

{{/* Request-state keys: the user's own Secret, or the chart's. Empty when there are none. */}}
{{- define "stroom-mcp.requestStateSecretName" -}}
{{- $rs := .Values.requestState }}
{{- if $rs.existingSecret }}{{ $rs.existingSecret }}{{ else if $rs.keys }}{{ include "stroom-mcp.secretName" . }}{{ end }}
{{- end }}

{{- define "stroom-mcp.requestStateSecretKey" -}}
{{- if .Values.requestState.existingSecret }}{{ .Values.requestState.existingSecretKey }}{{ else }}request-state-keys{{ end }}
{{- end }}

{{/* TLS Secret: the user's own, or the one cert-manager issues into. Fails if neither is set. */}}
{{- define "stroom-mcp.tlsSecretName" -}}
{{- if .Values.tls.existingSecret }}
{{- .Values.tls.existingSecret }}
{{- else if .Values.tls.certManager.enabled }}
{{- printf "%s-tls" (include "stroom-mcp.fullname" .) }}
{{- else }}
{{- fail "tls.enabled needs tls.existingSecret or tls.certManager.enabled (or set tls.enabled=false when TLS is terminated in front of the server)" }}
{{- end }}
{{- end }}

{{- define "stroom-mcp.portName" -}}
{{- if .Values.tls.enabled }}https{{ else }}http{{ end }}
{{- end }}

{{- define "stroom-mcp.servicePort" -}}
{{- if .Values.service.port }}
{{- .Values.service.port }}
{{- else if .Values.tls.enabled }}
{{- 443 }}
{{- else }}
{{- 80 }}
{{- end }}
{{- end }}

{{- define "stroom-mcp.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{/* A YAML file given as text or as a map. */}}
{{- define "stroom-mcp.yamlFile" -}}
{{- if kindIs "string" . }}
{{- . }}
{{- else }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{/* A CA volume from a Secret or a ConfigMap. Call with (dict "name" ... "ca" ...). */}}
{{- define "stroom-mcp.caVolume" -}}
{{- if .ca.secretName }}
- name: {{ .name }}
  secret:
    secretName: {{ .ca.secretName }}
{{- else if .ca.configMapName }}
- name: {{ .name }}
  configMap:
    name: {{ .ca.configMapName }}
{{- end }}
{{- end }}
