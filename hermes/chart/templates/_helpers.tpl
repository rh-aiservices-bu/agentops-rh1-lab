{{- define "hermes-openshell.fullname" -}}
hermes-openshell
{{- end -}}

{{- define "hermes-openshell.labels" -}}
app.kubernetes.io/name: hermes-openshell
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
