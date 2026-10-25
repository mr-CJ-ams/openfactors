{% macro revision_precedence() %}
    source_updated_at DESC NULLS LAST,
    retrieved_at DESC,
    artifact_id DESC
{% endmacro %}
