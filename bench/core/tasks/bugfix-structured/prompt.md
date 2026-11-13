Users of the `laya.structured` helpers in this repository report two problems with integer (score) fields in JSON schemas:

1. A field declared with `"minimum": 1, "maximum": 5` only offers the levels 1 to 4.
2. When an answer has no probabilities, `decide()` returns values that ignore the field's minimum: a field with `"minimum": 3` comes back as 0 for its lowest level instead of 3.

`python tests/test_structured.py` currently fails. Find and fix the root cause in the library code. Do not modify anything under `tests/`. Run the relevant tests before you finish.
