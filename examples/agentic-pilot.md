# Agentic pilot: brief, plan, execution and proof

Run from the repository with QGIS installed:

```bash
.venv/Scripts/python examples/agentic_pilot.py
```

The script creates all inputs itself and follows the product sequence rather than
calling an engine directly: a typed plan, `brief_plan` after validation, then
`execute_plan`. QGIS `native:buffer` creates the 20 m road corridor; overlays
select candidates and remove overhead lines; `measure_area` computes the final
area. MapSmith writes the canonical output and the plan manifest.

Expected and measured answer on 2026-10-28:

- 2 suitable features;
- areas 400 m² and 900 m², total 1,300 m²;
- first operation executed by QGIS;
- every step `ok`, with the plan manifest beside the answer.

`tests/test_agentic_pilot.py` executes the same flow and reads the manifest. The
manifest proves what ran and makes the analysis replicable; it does not certify
that the planning choices are right for every real site.

## Fine-tuning decision

No proprietary-model fine-tuning now. The stable post-#27 method-choice bench
has mixed results: calibration exact 6/10 (was 7), evaluation exact 6/10 (was 3),
output 6/10 on both, assumptions and reproducibility still 0/10. That is enough
to keep improving contracts and executable cases, not evidence that training a
model will improve the product. Revisit only against a new complete baseline
with a measured advantage and a defined cost.
