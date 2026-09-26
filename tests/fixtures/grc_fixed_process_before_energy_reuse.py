"""Frozen pre-accounting-reuse production process; comparison oracle only."""
import math
import numpy as np
UINT64_MAX = (1 << 64) - 1

def process(self, iq):
    if (not isinstance(iq, np.ndarray) or iq.dtype != np.complex64
            or iq.ndim != 1 or not np.isfinite(iq).all()):
        raise ValueError("fixed-reference input must be finite one-dimensional cf32")
    n = len(iq)
    fixed = self.mode == 'fixed'
    masked = n if fixed and self.config['gain'] == 0.0 else 0
    attenuated = n if fixed and 0.0 < self.config['gain'] < 1.0 else 0
    increments = {
        'sample_clock': n, 'awgn_complex_draws': n if fixed else 0,
        'awgn_normal_draws': 2 * n if fixed else 0,
        'masked_samples': masked, 'attenuated_samples': attenuated,
    }
    for name, increment in increments.items():
        if getattr(self, name) > UINT64_MAX - increment:
            raise OverflowError(f"fixed-reference {name} exceeds uint64")
    if not fixed:
        desired = output = iq
        energies = {'input': self._energy(iq), 'noise': 0.0, 'cw': 0.0}
        energies['desired'] = energies['output'] = energies['input']
    else:
        desired = np.empty(n, dtype=np.complex64)
        np.multiply(iq.real, self.config['gain'], out=desired.real, dtype=np.float64)
        np.multiply(iq.imag, self.config['gain'], out=desired.imag, dtype=np.float64)
        # Consume the same two float32 normal variates per complex sample,
        # even with noise disabled, a zero input, or a desired-signal mask.
        noise_reals = self.rng.standard_normal(2 * n, dtype=np.float32)
        if self.config['noise_enabled']:
            np.multiply(noise_reals, self.noise_std, out=noise_reals, dtype=np.float64)
        else:
            noise_reals.fill(0.0)
        noise = noise_reals.view(np.complex64)
        phases = np.arange(n, dtype=np.uint64)
        phases *= np.uint64(self.cw_step_u64)
        phases += np.uint64(self.phase_u64)
        cw = np.zeros(n, dtype=np.complex64)
        if self.config['cw_enabled']:
            angles = np.ldexp(phases.astype(np.float64), -64) * (2.0 * math.pi)
            np.multiply(np.cos(angles), self.cw_amplitude, out=cw.real)
            np.multiply(np.sin(angles), self.cw_amplitude, out=cw.imag)
        output = np.empty(n, dtype=np.complex64)
        output.real = desired.real.astype(np.float64) + cw.real + noise.real
        output.imag = desired.imag.astype(np.float64) + cw.imag + noise.imag
        if not np.isfinite(output).all():
            raise ValueError("fixed-reference processing produced nonfinite output")
        energies = {
            'input': self._energy(iq), 'desired': self._energy(desired),
            'noise': self._energy(noise), 'cw': self._energy(cw),
            'output': self._energy(output),
        }
    for name, energy in energies.items():
        if not math.isfinite(self.energies[name] + energy):
            raise OverflowError("fixed-reference cumulative energy exceeds binary64")
    for name, increment in increments.items():
        setattr(self, name, getattr(self, name) + increment)
    for name, energy in energies.items():
        self.energies[name] += energy
    if fixed:
        self.phase_u64 = (self.phase_u64 + n * self.cw_step_u64) & UINT64_MAX
    return output
