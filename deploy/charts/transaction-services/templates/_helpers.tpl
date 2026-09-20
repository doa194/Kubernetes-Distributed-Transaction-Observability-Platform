{{/* Labels shared by every resource of one service. */}}
{{- define "txs.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: txplatform
app.kubernetes.io/component: application
app.kubernetes.io/version: {{ .service.tag | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end }}

{{/* Labels Deployments and Services select pods by. */}}
{{- define "txs.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}
