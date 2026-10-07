# corral-light-finops

What your Corral Light lanes cost, and how much quota is left.

This is the first out-of-tree module for
[Corral Light](https://github.com/cvp1/corral-light). It is installed
beside Light, never inside it:

```
corral-light module add finops
```

## Status

Not usable yet. The module seam in Corral Light is built (Phase 1).
This repository will hold the module itself (Phase 2): its collector,
its `setup` and its doctor. Until a `module.json` lands here,
`module add finops` refuses to install it.

The design, including what the module may read, how it is sandboxed
and what it shows, is in Light's
[docs/finops-module-plan.md](https://github.com/cvp1/corral-light/blob/master/docs/finops-module-plan.md).

## License

MIT, see [LICENSE](LICENSE).
