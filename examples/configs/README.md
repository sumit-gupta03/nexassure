# Connection examples

Copy the block for your warehouse into the `connections:` list of your
`nexassure.yml`. Every secret is an `${env:VAR}` reference, so these files are safe
to commit as-is.

See [docs/connectors.md](../../docs/connectors.md) for per-engine behaviour and
the minimum grants each one needs.
