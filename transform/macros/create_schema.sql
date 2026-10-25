{% macro postgres__create_schema(relation) %}
  {# Schemas are created by the local Postgres init script. of_transform cannot create them. #}
{% endmacro %}
