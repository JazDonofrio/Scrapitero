"""
Agentes especializados de Scrapitero.

Cada agente es una función pura tipada (Pydantic in/out) que
encapsula UNA responsabilidad atómica. No usan `input()` ni
`print()` ni tocan el filesystem implícitamente.

Pueden invocarse desde:
  - notebooks (importando la función directo)
  - wrappers CLI (scrapitero/cli/*)
  - el orquestador-LLM (registradas como tools en el loop tool-use)

Ver PROYECTO_CONTEXTO.md Anexo B para el catálogo completo.
"""
