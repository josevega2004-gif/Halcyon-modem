// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Halcyon contributors
// halcyon-modem -- modulate, demodulate and self-test the C++ port.

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <random>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

#include "halcyon.hpp"
#include "halcyon_tables.hpp"

namespace {

// The table file must not depend on the caller's working directory: a packaged
// binary lives in /usr/bin, a clone build in cpp/build, and both should find it.
std::string exe_dir() {
  char buf[4096];
  const ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
  if (n <= 0) return {};
  buf[n] = '\0';
  const std::string p(buf);
  const auto slash = p.find_last_of('/');
  return slash == std::string::npos ? std::string() : p.substr(0, slash);
}

bool is_file(const std::string& path) {
  struct stat st;
  return ::stat(path.c_str(), &st) == 0 && S_ISREG(st.st_mode);
}

std::string resolve_data(const std::string& explicit_path) {
  if (!explicit_path.empty()) return explicit_path;
  if (const char* env = std::getenv("HALCYON_DATA"); env != nullptr && *env != '\0') return env;
  std::vector<std::string> candidates;
  const std::string dir = exe_dir();
  if (!dir.empty()) {
    candidates.push_back(dir + "/data/wide.bin");            // clone: cpp/build + data
    candidates.push_back(dir + "/../data/wide.bin");
    candidates.push_back(dir + "/../share/halcyon/wide.bin");  // package: /usr/bin + share
  }
  candidates.push_back("/usr/share/halcyon/wide.bin");
  candidates.push_back("/usr/local/share/halcyon/wide.bin");
  candidates.push_back("cpp/data/wide.bin");  // running from the repo root
  candidates.push_back("data/wide.bin");      // running from cpp/
  for (const auto& path : candidates) {
    if (is_file(path)) return path;
  }
  return candidates.empty() ? std::string("cpp/data/wide.bin") : candidates.front();
}

std::string json_escape(const std::string& s) {
  std::string out;
  for (unsigned char c : s) {
    if (c == '"' || c == '\\') {
      out.push_back('\\');
      out.push_back(static_cast<char>(c));
    } else if (c >= 32 && c < 127) {
      out.push_back(static_cast<char>(c));
    } else {
      char buf[8];
      std::snprintf(buf, sizeof(buf), "\\u%04x", c);
      out += buf;
    }
  }
  return out;
}

void usage() {
  std::puts(
      "usage: halcyon-modem <command> [options]\n"
      "  rungs                          list the rate ladder\n"
      "  tx  --mcs N --text S --out F   modulate a frame (complex float32)\n"
      "  rx  --in F                     demodulate a capture, JSON on stdout\n"
      "  selftest                       modulate and decode every rung\n"
      "  bench --mcs N --snr DB --frames F\n"
      "                                 AWGN block-error bench (add --fading)\n"
      "\n"
      "options: --data PATH   table file (default: next to the binary,\n"
      "                        /usr/share/halcyon, or cpp/data)\n"
      "         --seq N       frame sequence number (tx)\n"
      "         --decoder M   sumprod (default), ms, sposd, msosd\n"
      "         --iters N     payload BP iterations (default 40)\n"
      "         --osd-flips N OSD order after a failed BP (default 2 when enabled)\n"
      "         --llr M       channel (per-|H| scaling, default) or flat\n"
      "         --blocks N    bench payload size in blocks (default 4)\n");
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    usage();
    return 2;
  }
  const std::string cmd = argv[1];
  std::string data;
  std::string text = "test de halcyon k";
  std::string in_path, out_path;
  int mcs = 4;
  int seq = 0;
  int repeat = 1;
  int iters = 40;
  int osd_flips = 2;
  int blocks = 4;
  int frames = 10;
  double snr = 3.0;
  bool fading = false;
  std::string decoder = "sumprod";
  std::string llr_mode = "channel";

  for (int i = 2; i < argc; ++i) {
    const std::string key = argv[i];
    const bool has_val = i + 1 < argc;
    if (key == "--data" && has_val) {
      data = argv[++i];
    } else if (key == "--text" && has_val) {
      text = argv[++i];
    } else if (key == "--in" && has_val) {
      in_path = argv[++i];
    } else if (key == "--out" && has_val) {
      out_path = argv[++i];
    } else if (key == "--mcs" && has_val) {
      mcs = std::atoi(argv[++i]);
    } else if (key == "--seq" && has_val) {
      seq = std::atoi(argv[++i]);
    } else if (key == "--repeat" && has_val) {
      repeat = std::max(1, std::atoi(argv[++i]));
    } else if (key == "--decoder" && has_val) {
      decoder = argv[++i];
    } else if (key == "--iters" && has_val) {
      iters = std::max(1, std::atoi(argv[++i]));
    } else if (key == "--osd-flips" && has_val) {
      osd_flips = std::max(0, std::atoi(argv[++i]));
    } else if (key == "--llr" && has_val) {
      llr_mode = argv[++i];
    } else if (key == "--blocks" && has_val) {
      blocks = std::max(1, std::atoi(argv[++i]));
    } else if (key == "--frames" && has_val) {
      frames = std::max(1, std::atoi(argv[++i]));
    } else if (key == "--snr" && has_val) {
      snr = std::atof(argv[++i]);
    } else if (key == "--fading") {
      fading = true;
    } else {
      std::fprintf(stderr, "unknown option: %s\n", key.c_str());
      return 2;
    }
  }

  data = resolve_data(data);

  try {
    halcyon::Modem modem(data);
    halcyon::DecodeConfig cfg;
    cfg.max_iter = iters;
    cfg.channel_llr = (llr_mode != "flat");
    if (decoder == "ms" || decoder == "msosd") cfg.min_sum = true;
    if (decoder == "sposd" || decoder == "msosd") cfg.osd_flips = osd_flips;
    if (decoder != "sumprod" && decoder != "ms" && decoder != "sposd" &&
        decoder != "msosd") {
      std::fprintf(stderr, "unknown decoder: %s\n", decoder.c_str());
      return 2;
    }
    modem.set_decode(cfg);

    if (cmd == "rungs") {
      std::printf("%-3s %-16s %6s %6s %6s %6s %10s\n", "#", "mode", "bps", "n", "k", "rep",
                  "bytes/burst");
      for (int i = 0; i < modem.rung_count(); ++i) {
        const auto r = modem.rung(i);
        std::printf("%-3d %-16s %6d %6d %6d %6d %10.0f\n", i, r.name.c_str(), r.bps, r.n, r.k,
                    r.rep, r.max_bytes);
      }
      return 0;
    }

    if (cmd == "tx") {
      if (out_path.empty()) {
        std::fprintf(stderr, "tx needs --out\n");
        return 2;
      }
      const auto built = modem.build(text, mcs, seq);
      halcyon::write_iq(out_path, built.wave);
      std::printf(
          "{\"mcs\":%d,\"blocks\":%d,\"samples\":%zu,\"seconds\":%.3f,\"out\":\"%s\"}\n",
          built.mcs, built.blocks, built.wave.size(), built.airtime_s,
          json_escape(out_path).c_str());
      return 0;
    }

    if (cmd == "rx") {
      if (in_path.empty()) {
        std::fprintf(stderr, "rx needs --in\n");
        return 2;
      }
      const auto rx = halcyon::read_iq(in_path);
      const auto t0 = std::chrono::steady_clock::now();
      halcyon::Result res;
      for (int n = 0; n < repeat; ++n) res = modem.receive(rx);
      const double ms =
          std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
              .count() /
          std::max(1, repeat);
      std::string blocks;
      for (size_t i = 0; i < res.blocks_ok.size(); ++i) {
        blocks += (i ? "," : "");
        blocks += res.blocks_ok[i] ? "true" : "false";
      }
      std::printf(
          "{\"ok\":%s,\"header_ok\":%s,\"text\":\"%s\",\"mcs\":%d,\"blocks\":%d,"
          "\"blocks_ok\":[%s],\"snr_db\":%.2f,\"cfo_hz\":%.2f,\"start\":%d,\"seq\":%d,"
          "\"iters\":%d,\"ms\":%.2f}\n",
          res.ok ? "true" : "false", res.header_ok ? "true" : "false",
          json_escape(res.payload).c_str(), res.mcs, res.blocks, blocks.c_str(), res.snr_db,
          res.cfo_hz, res.sample_start, res.seq, res.header_iters, ms);
      return res.ok ? 0 : 1;
    }

    if (cmd == "bench") {
      if (mcs < 0 || mcs >= modem.rung_count()) {
        std::fprintf(stderr, "no such rung: %d\n", mcs);
        return 2;
      }
      const auto info = modem.rung(mcs);
      const int cap = info.k - 16;
      const int bytes = std::max(0, cap * blocks / 8);
      std::string payload;
      payload.reserve(static_cast<size_t>(bytes));
      for (int i = 0; i < bytes; ++i) {
        payload.push_back(static_cast<char>('a' + (i * 7) % 26));
      }
      std::mt19937 rng(12345);
      std::normal_distribution<double> nd(0.0, 1.0);
      int seen = 0, bad = 0, good_frames = 0;
      double ms_sum = 0.0;
      for (int f = 0; f < frames; ++f) {
        auto built = modem.build(payload, mcs, f & 0xFF);
        auto wave = built.wave;
        if (fading) {
          // Two-tap channel: a 0.7 echo eight samples (667 us) behind the
          // direct path.  Coherence bandwidth ~1.5 kHz, so the 2.75 kHz
          // signal is genuinely frequency-selective.
          const halcyon::cpx echo = 0.7 * std::exp(halcyon::cpx(0.0, 1.1));
          for (size_t i = wave.size(); i-- > 8;) wave[i] += echo * wave[i - 8];
        }
        double power = 0.0;
        for (const auto& v : wave) power += std::norm(v);
        power /= std::max<size_t>(1, wave.size());
        // S/N is quoted in the signal's occupied bandwidth (117 carriers at
        // 23.4375 Hz = 2742.1875 Hz), as halcyon/channel.py does; a sample-rate
        // variance would read 10*log10(12000/2742) = 6.4 dB too easy.
        const double bandwidth = 117.0 * (static_cast<double>(halcyon::kFs) / halcyon::kFft);
        const double noise = power * (halcyon::kFs / bandwidth) /
                             std::pow(10.0, snr / 10.0);
        const double sigma = std::sqrt(noise / 2.0);
        for (auto& v : wave) v += halcyon::cpx(sigma * nd(rng), sigma * nd(rng));
        const auto t0 = std::chrono::steady_clock::now();
        const auto res = modem.receive(wave);
        ms_sum += std::chrono::duration<double, std::milli>(
                      std::chrono::steady_clock::now() - t0)
                      .count();
        for (bool ok : res.blocks_ok) {
          ++seen;
          if (!ok) ++bad;
        }
        if (res.ok && res.payload == payload) ++good_frames;
      }
      std::printf(
          "{\"mcs\":%d,\"snr\":%.2f,\"fading\":%s,\"decoder\":\"%s\",\"llr\":\"%s\","
          "\"frames\":%d,\"blocks\":%d,\"bler\":%.4f,\"fer\":%.4f,\"ms\":%.2f}\n",
          mcs, snr, fading ? "true" : "false", decoder.c_str(), llr_mode.c_str(), frames,
          seen, seen ? static_cast<double>(bad) / seen : 1.0,
          frames ? 1.0 - static_cast<double>(good_frames) / frames : 1.0,
          ms_sum / std::max(1, frames));
      return bad == 0 ? 0 : 1;
    }

    if (cmd == "selftest") {
      int bad = 0;
      const std::string payload = "test de halcyon k";
      std::printf("%-3s %-16s %8s %7s %s\n", "#", "mode", "airtime", "snr", "result");
      for (int i = 0; i < modem.rung_count(); ++i) {
        const auto built = modem.build(payload, i);
        const auto res = modem.receive(built.wave);
        const bool pass = res.ok && res.payload == payload;
        if (!pass) ++bad;
        std::printf("%-3d %-16s %7.2fs %+6.1f %s%s\n", i, modem.rung(i).name.c_str(),
                    built.airtime_s, res.snr_db, pass ? "PASS" : "FAIL",
                    pass ? "" : ("  header=" + std::string(res.header_ok ? "ok" : "lost") +
                                 " got=\"" + res.payload + "\"")
                              .c_str());
      }
      std::printf("%d/%d rungs round-trip\n", modem.rung_count() - bad, modem.rung_count());
      return bad == 0 ? 0 : 1;
    }

    usage();
    return 2;
  } catch (const std::exception& exc) {
    std::fprintf(stderr, "error: %s\n", exc.what());
    return 3;
  }
}
