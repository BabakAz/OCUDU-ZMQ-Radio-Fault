#!/usr/bin/env python3
"""
validate_broker.py — Standalone validation for ocudu_channel_broker.py

Tests the impairment chain (FadingState, FrequencySelectiveFading, add_awgn,
apply_cfo) in isolation with synthetic IQ data. The actual production module
and numerical/transport Python dependencies must import successfully. No socket
or OCUDU runtime is started; GNU Radio/Qt are needed only for the optional GUI.

Usage:
    python3 validate_broker.py           # full sweep
    python3 validate_broker.py --quick   # reduced sweep (faster)
    python3 validate_broker.py --live    # also tests dynamic slider changes
"""

import sys
import math
import argparse
import numpy as np
from itertools import product

# ── Import the actual headless-capable broker module ────────────────────────
# GUI dependencies are imported only by the broker's explicit GUI factory.
# A production import error is fatal; partial modules are never accepted.
import importlib.util
import os


def load_broker_module(path):
    spec = importlib.util.spec_from_file_location("broker_validation_source", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load broker source from {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


broker_path = os.environ.get(
    "GRC_BROKER_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocudu_channel_broker.py"),
)
broker_mod = load_broker_module(broker_path)

FadingState = broker_mod.FadingState
FrequencySelectiveFading = broker_mod.FrequencySelectiveFading
add_awgn = broker_mod.add_awgn
apply_cfo = broker_mod.apply_cfo
DELAY_PROFILES = broker_mod.DELAY_PROFILES
FADING_MODES = broker_mod.FADING_MODES


# ── Test helpers ─────────────────────────────────────────────────────────────

SAMP_RATE = 23.04e6
SUBFRAME_SAMPLES = int(SAMP_RATE * 0.001)   # 1 ms subframe = 23040 samples
N_SUBFRAMES = 20                             # simulate 20 subframes per test

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results = []   # list of (label, status, detail)


def fake_iq(n=SUBFRAME_SAMPLES, power_dbfs=-10.0):
    """Generate a synthetic random QPSK fixture, not a compliant NR waveform."""
    rng = np.random.default_rng(0)
    scale = 10 ** (power_dbfs / 20.0)
    re = rng.choice([-1.0, 1.0], n).astype(np.float32) * scale
    im = rng.choice([-1.0, 1.0], n).astype(np.float32) * scale
    return (re + 1j * im).astype(np.complex64)


def check_iq(iq, label):
    """Return (ok, detail) — True if IQ has no NaN/Inf and nonzero power."""
    if np.any(np.isnan(iq)):
        return False, "NaN detected"
    if np.any(np.isinf(iq)):
        return False, "Inf detected"
    power = float(np.mean(np.abs(iq) ** 2))
    if power < 1e-30:
        return False, f"zero power ({power:.2e})"
    return True, f"power={10*math.log10(power):.1f} dBfs"


def record(label, ok, detail, warn_only=False):
    status = PASS if ok else (WARN if warn_only else FAIL)
    tag = "PASS" if ok else ("WARN" if warn_only else "FAIL")
    results.append((label, tag, detail))
    sym = "✓" if ok else ("△" if warn_only else "✗")
    print(f"  {sym} {label}: {detail}")


# ── Test 1: AWGN alone ───────────────────────────────────────────────────────

def test_awgn():
    print("\n[1] AWGN (add_awgn) — SNR sweep")
    rng = np.random.default_rng(1)
    snr_values = [5, 8, 10, 12, 15, 20, 25, 28, 30, 35, 40]
    for snr_db in snr_values:
        iq = fake_iq()
        snr_lin = 10 ** (snr_db / 10.0)
        try:
            out = add_awgn(iq, snr_lin, rng)
            ok, detail = check_iq(out, f"SNR={snr_db}dB")
            record(f"AWGN SNR={snr_db}dB", ok, detail)
        except Exception as e:
            record(f"AWGN SNR={snr_db}dB", False, f"EXCEPTION: {e}")


# ── Test 2: Flat fading (FadingState) ───────────────────────────────────────

def test_flat_fading():
    print("\n[2] Flat fading (FadingState) — K-factor × Doppler sweep")
    k_values  = [-10, -5, 0, 3, 5, 10, 15, 20]
    dop_values = [0.1, 1, 5, 10, 50, 100, 200, 300]
    rng_seed = 2

    for k_db, dop in product(k_values, dop_values):
        rng = np.random.default_rng(rng_seed)
        try:
            fs = FadingState(True, dop, SAMP_RATE, k_db, rng)
            iq = fake_iq()
            ok_all = True
            detail = ""
            for _ in range(N_SUBFRAMES):
                out = fs.update_and_apply(iq)
                ok, d = check_iq(out, "")
                if not ok:
                    ok_all = False
                    detail = d
                    break
            if ok_all:
                detail = f"ok over {N_SUBFRAMES} subframes"
            record(f"Flat-fading K={k_db}dB fd={dop}Hz", ok_all, detail)
        except Exception as e:
            record(f"Flat-fading K={k_db}dB fd={dop}Hz", False, f"EXCEPTION: {e}")

    print("\n  [Rayleigh — K=-100 dB (mode 2)]")
    for dop in [1, 5, 70, 300]:
        rng = np.random.default_rng(3)
        try:
            fs = FadingState(True, dop, SAMP_RATE, -100.0, rng)
            iq = fake_iq()
            ok_all = True
            detail = ""
            for _ in range(N_SUBFRAMES):
                out = fs.update_and_apply(iq)
                ok, d = check_iq(out, "")
                if not ok:
                    ok_all = False
                    detail = d
                    break
            min_pow = min(float(np.mean(np.abs(fs.update_and_apply(fake_iq()))**2))
                          for _ in range(10))
            if ok_all:
                detail = f"ok, min_power={10*math.log10(max(min_pow,1e-30)):.1f}dBfs (deep fade possible)"
            record(f"Rayleigh fd={dop}Hz", ok_all, detail,
                   warn_only=(ok_all and min_pow < 1e-6))
        except Exception as e:
            record(f"Rayleigh fd={dop}Hz", False, f"EXCEPTION: {e}")


# ── Test 3: Frequency-selective fading ──────────────────────────────────────

def test_freq_selective():
    print("\n[3] Frequency-selective fading (EPA/EVA/ETU) — Doppler sweep")
    profiles = ['epa', 'eva', 'etu']
    dop_values = [1, 5, 10, 50, 70, 100, 200, 300]
    for prof, dop in product(profiles, dop_values):
        rng = np.random.default_rng(4)
        try:
            fs = FrequencySelectiveFading(prof, SAMP_RATE, dop, rng)
            iq = fake_iq()
            ok_all = True
            detail = ""
            for _ in range(N_SUBFRAMES):
                out = fs.update_and_apply(iq)
                ok, d = check_iq(out, "")
                if not ok:
                    ok_all = False
                    detail = d
                    break
            if ok_all:
                out_power = 10*math.log10(float(np.mean(np.abs(out)**2)) + 1e-30)
                detail = f"ok, out_power={out_power:.1f}dBfs"
            record(f"{prof.upper()} fd={dop}Hz", ok_all, detail)
        except Exception as e:
            record(f"{prof.upper()} fd={dop}Hz", False, f"EXCEPTION: {e}")


# ── Test 4: CFO ──────────────────────────────────────────────────────────────

def test_cfo():
    print("\n[4] CFO (apply_cfo) — frequency offset sweep")
    cfo_values = [0, 1, 10, 50, 100, 200, 300, 500]
    phase_state = [0.0]
    for cfo in cfo_values:
        phase_state[0] = 0.0
        iq = fake_iq()
        try:
            out = apply_cfo(iq, float(cfo), SAMP_RATE, phase_state)
            ok, detail = check_iq(out, f"CFO={cfo}Hz")
            # CFO should preserve power
            in_pow  = float(np.mean(np.abs(iq)**2))
            out_pow = float(np.mean(np.abs(out)**2))
            power_err = abs(out_pow - in_pow) / (in_pow + 1e-30)
            if power_err > 0.01:
                detail += f" (power error {power_err*100:.2f}% — unexpected)"
                ok = False
            record(f"CFO={cfo}Hz", ok, detail)
        except Exception as e:
            record(f"CFO={cfo}Hz", False, f"EXCEPTION: {e}")


# ── Test 5: Combined chain ───────────────────────────────────────────────────

def test_combined():
    print("\n[5] Combined impairment chain — known good and boundary combos")
    combos = [
        # label,               snr_db, k_db,  dop,  mode,  cfo,  drop
        ("baseline",           28.0,   3.0,   5.0,  1,     0.0,  0.0),
        ("low-snr",            10.0,   3.0,   5.0,  1,     0.0,  0.0),
        ("very-low-snr",        5.0,   3.0,   5.0,  1,     0.0,  0.0),
        ("high-snr",           40.0,   3.0,   5.0,  1,     0.0,  0.0),
        ("rayleigh",           28.0, -100.0,  5.0,  2,     0.0,  0.0),
        ("high-doppler",       28.0,   3.0, 300.0,  1,     0.0,  0.0),
        ("epa-low-dop",        28.0,   3.0,   5.0,  3,     0.0,  0.0),
        ("eva-mid-dop",        28.0,   3.0,  70.0,  4,     0.0,  0.0),
        ("etu-high-dop",       28.0,   3.0, 300.0,  5,     0.0,  0.0),
        ("cfo-high",           28.0,   3.0,   5.0,  1,   500.0,  0.0),
        ("drop-5pct",          28.0,   3.0,   5.0,  1,     0.0,  0.05),
        ("drop-20pct",         28.0,   3.0,   5.0,  1,     0.0,  0.20),
        ("drop-25pct",         28.0,   3.0,   5.0,  1,     0.0,  0.25),
        ("worst-case",          8.0, -100.0, 300.0,  2,   500.0,  0.25),
        ("edge-of-cell-end",    8.0,   3.0,   5.0,  1,     0.0,  0.10),
        ("drive-by-peak",      15.0,   3.0, 200.0,  1,     0.0,  0.02),
        ("k-neg10",            28.0, -10.0,   5.0,  1,     0.0,  0.0),
        ("k-pos20",            28.0,  20.0,   5.0,  1,     0.0,  0.0),
    ]

    rng_base = np.random.default_rng(5)

    for (label, snr_db, k_db, dop, mode, cfo, drop) in combos:
        rng = np.random.default_rng(6)
        phase_state = [0.0]
        try:
            # Build fading
            if mode in (3, 4, 5):
                prof = {3: 'epa', 4: 'eva', 5: 'etu'}[mode]
                fader = FrequencySelectiveFading(prof, SAMP_RATE, dop, rng)
            elif mode == 2:
                fader = FadingState(True, dop, SAMP_RATE, -100.0, rng)
            elif mode == 1:
                fader = FadingState(True, dop, SAMP_RATE, k_db, rng)
            else:
                fader = FadingState(False, dop, SAMP_RATE, k_db, rng)

            snr_lin = 10 ** (snr_db / 10.0)
            ok_all = True
            detail = ""
            dropped = 0

            for sf in range(N_SUBFRAMES):
                iq = fake_iq()
                # Drop
                if drop > 0.0 and rng_base.random() < drop:
                    iq = np.zeros_like(iq)
                    dropped += 1
                    continue
                # Fading
                iq = fader.update_and_apply(iq)
                # CFO
                iq = apply_cfo(iq, cfo, SAMP_RATE, phase_state)
                # Match the live chain: AWGN is referenced to post-channel
                # desired power for flat and frequency-selective profiles.
                desired_power = float(np.mean(np.abs(iq) ** 2))
                iq = add_awgn(iq, snr_lin, rng, signal_power=desired_power)

                ok, d = check_iq(iq, label)
                if not ok:
                    ok_all = False
                    detail = d
                    break

            if ok_all:
                detail = f"ok ({N_SUBFRAMES} subframes"
                if dropped:
                    detail += f", {dropped} dropped"
                detail += ")"
            record(f"combo:{label}", ok_all, detail)
        except Exception as e:
            record(f"combo:{label}", False, f"EXCEPTION: {e}")


# ── Test 6: Dynamic parameter changes (slider simulation) ────────────────────

def test_dynamic_changes():
    print("\n[6] Dynamic parameter changes — simulating live slider moves")
    rng = np.random.default_rng(7)

    # Simulate the channel_broker_source internals
    class MockBroker:
        def __init__(self):
            self.samp_rate = SAMP_RATE
            self.doppler_hz = 5.0
            self.k_factor_db = 3.0
            self.fading_mode = 1
            self._dl_rng = np.random.default_rng(42)
            self._ul_rng = np.random.default_rng(137)
            fading = FadingState(True, 5.0, SAMP_RATE, 3.0, self._dl_rng)
            self._dl_imp = {
                'snr_linear': [10**2.8],
                'fading':     [fading],
                'cfo_hz':     [0.0],
                'cfo_phase':  [0.0],
                'drop_prob':  [0.0],
            }

        def set_snr_db(self, val):
            snr_lin = pow(10.0, val / 10.0)
            self._dl_imp['snr_linear'][0] = snr_lin

        def set_k_factor_db(self, val):
            self.k_factor_db = val
            if self.fading_mode <= 2:
                k = -100.0 if self.fading_mode == 2 else val
                self._dl_imp['fading'][0].reconfigure(
                    self.fading_mode >= 1, self.doppler_hz, k)

        def set_doppler_hz(self, val):
            self.doppler_hz = val
            if self.fading_mode <= 2:
                k = -100.0 if self.fading_mode == 2 else self.k_factor_db
                self._dl_imp['fading'][0].reconfigure(
                    self.fading_mode >= 1, val, k)
            else:
                self._dl_imp['fading'][0].reconfigure(True, val)

        def set_fading_mode(self, val):
            self.fading_mode = val
            if val in (3, 4, 5):
                prof = {3: 'epa', 4: 'eva', 5: 'etu'}[val]
                self._dl_imp['fading'][0] = FrequencySelectiveFading(
                    prof, self.samp_rate, self.doppler_hz, self._dl_rng)
            elif val == 2:
                self._dl_imp['fading'][0] = FadingState(
                    True, self.doppler_hz, self.samp_rate, -100.0, self._dl_rng)
            elif val == 1:
                self._dl_imp['fading'][0] = FadingState(
                    True, self.doppler_hz, self.samp_rate,
                    self.k_factor_db, self._dl_rng)
            else:
                self._dl_imp['fading'][0] = FadingState(
                    False, self.doppler_hz, self.samp_rate,
                    self.k_factor_db, self._dl_rng)

        def set_cfo_hz(self, val):
            self._dl_imp['cfo_hz'][0] = val

        def set_drop_prob(self, val):
            self._dl_imp['drop_prob'][0] = val

        def process(self, iq):
            imp = self._dl_imp
            if imp['drop_prob'][0] > 0 and rng.random() < imp['drop_prob'][0]:
                return np.zeros_like(iq)
            fader = imp['fading'][0]
            iq = fader.update_and_apply(iq)
            iq = apply_cfo(iq, imp['cfo_hz'][0], self.samp_rate, imp['cfo_phase'])
            desired_power = float(np.mean(np.abs(iq) ** 2))
            iq = add_awgn(
                iq, imp['snr_linear'][0], rng, signal_power=desired_power
            )
            return iq

    # Sequence of slider changes interleaved with processing
    change_sequences = [
        ("snr-sweep",   [("set_snr_db", v) for v in [40, 30, 20, 10, 5, 28]]),
        ("k-sweep",     [("set_k_factor_db", v) for v in [20, 10, 0, -10, 3]]),
        ("dop-sweep",   [("set_doppler_hz", v) for v in [300, 200, 100, 50, 5]]),
        ("mode-switch", [("set_fading_mode", v) for v in [0, 1, 2, 3, 4, 5, 1]]),
        ("cfo-sweep",   [("set_cfo_hz", v) for v in [500, 200, 0, -200, -500, 0]]),
        ("drop-sweep",  [("set_drop_prob", v) for v in [0.25, 0.1, 0.05, 0.01, 0.0]]),
        ("mixed",       [
            ("set_snr_db", 10), ("set_fading_mode", 3),
            ("set_doppler_hz", 70), ("set_cfo_hz", 100),
            ("set_drop_prob", 0.05), ("set_fading_mode", 1),
            ("set_snr_db", 28), ("set_cfo_hz", 0),
        ]),
    ]

    for seq_name, changes in change_sequences:
        b = MockBroker()
        ok_all = True
        detail = ""
        try:
            for (method, val) in changes:
                getattr(b, method)(val)
                for _ in range(3):   # process a few subframes after each change
                    iq = fake_iq()
                    out = b.process(iq)
                    # Dropped subframes are all-zeros — broker sends zeros intentionally
                    if len(out) == 0 or float(np.mean(np.abs(out)**2)) < 1e-30:
                        continue   # dropped — OK
                    ok, d = check_iq(out, "")
                    if not ok:
                        ok_all = False
                        detail = f"after {method}({val}): {d}"
                        break
                if not ok_all:
                    break
            if ok_all:
                detail = f"ok ({len(changes)} changes, 3 subframes each)"
        except Exception as e:
            ok_all = False
            detail = f"EXCEPTION: {e}"
        record(f"dynamic:{seq_name}", ok_all, detail)


# ── Summary ──────────────────────────────────────────────────────────────────

def print_summary():
    print("\n" + "=" * 65)
    print("  VALIDATION SUMMARY")
    print("=" * 65)
    passes  = [r for r in results if r[1] == "PASS"]
    warns   = [r for r in results if r[1] == "WARN"]
    fails   = [r for r in results if r[1] == "FAIL"]
    print(f"  Total: {len(results)}   PASS: {len(passes)}   WARN: {len(warns)}   FAIL: {len(fails)}")
    print("-" * 65)

    if fails:
        print(f"\n  {FAIL} FAILURES ({len(fails)}):")
        for label, _, detail in fails:
            print(f"    ✗ {label}")
            print(f"      → {detail}")

    if warns:
        print(f"\n  {WARN} WARNINGS ({len(warns)}) — risky but not crash:")
        for label, _, detail in warns:
            print(f"    △ {label}")
            print(f"      → {detail}")

    print("\n  OFFLINE VALIDATION SCOPE")
    print("  ─────────────────────────────────────────────────────────────────")
    print("  PASS proves numerical execution, finite output, and basic power checks.")
    print("  It does not prove UE attach, throughput, channel calibration, or safety.")
    print("  Only the clean-host flat-Rician route is functionally live-accepted;")
    print("  EPA/EVA/ETU and other impairment combinations remain live-gated.")
    print("=" * 65)

    return len(fails) == 0


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Validate ocudu_channel_broker.py impairment chain")
    parser.add_argument("--quick", action="store_true", help="Reduced sweep (fewer combos)")
    parser.add_argument("--live",  action="store_true", help="Include dynamic slider tests")
    args = parser.parse_args()

    print("OCUDU Channel Broker — Impairment Chain Validator")
    print("=" * 65)
    print(f"  Sample rate:    {SAMP_RATE/1e6:.2f} MHz")
    print(f"  Subframe size:  {SUBFRAME_SAMPLES} samples (1 ms)")
    print(f"  Subframes/test: {N_SUBFRAMES}")
    print("=" * 65)

    test_awgn()
    test_flat_fading()
    test_freq_selective()
    test_cfo()
    test_combined()
    test_dynamic_changes()

    ok = print_summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
