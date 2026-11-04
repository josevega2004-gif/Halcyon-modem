# Credits

Halcyon was written from published specifications. No third-party source code
is included in this repository and none was copied into it, so the credits below
name the documents and programs that made the work possible, or that Halcyon is
built to talk to. Libraries that are actually used are listed separately.

## Protocol documents

- **TCI (Transceiver Control Interface)**, Expert Electronics — *TCI Protocol
  2.1* and the README in [ExpertSDR3/TCI](https://github.com/ExpertSDR3/TCI).
  The command grammar and the 64-byte stream header in `halcyon/tci.py` follow
  that document, including its `Stream.length` definition.
- **Hermes-Lite 2** — the protocol pages and gateware description in
  [softerhardware/Hermes-Lite2](https://github.com/softerhardware/Hermes-Lite2),
  which describe the HPSDR Protocol 1 framing used by `halcyon/hl2.py`.
- **HPSDR / openHPSDR** — the Metis discovery broadcast and start/stop packets
  originate in the openHPSDR protocol, documented by the TAPR OpenHPSDR
  project.

## Reference implementations

Compatibility was checked against how these behave on the wire. No code was
taken from them, and none of them is required to build or run Halcyon.

- **PAT** — [la5nta/pat](https://github.com/la5nta/pat), the Winlink client
  whose host-mode command set and dialling behaviour shaped the host service.
- **VarAC** — [varac-hamradio.com](https://www.varac-hamradio.com/), the HF chat
  client that drives the same two ports.
- **Quisk** and **deskHPSDR** — TCI clients whose behaviour informed the text
  commands and audio frame handling.
- **Hamlib** — [hamlib.github.io](https://hamlib.github.io/); the CAT panel
  drives `rigctld` as an external program rather than linking the library.

## Libraries

- **NumPy** and **SciPy** — array and signal-processing work throughout the
  modem; the NumPy and SciPy developers, under their own licences.
- **websockets** — Aymeric Augustin and contributors; used only by the optional
  TCI client, under its own licence.
- The Python standard library, and `dpkg`/`lintian` in the packaging scripts.

## Standards and literature

- CRC-16/CCITT-FALSE is used as specified for that CRC.
- The LDPC construction and sum-product decoder, and the OFDM/PAPR machinery,
  are implemented here from the standard descriptions; see the *Design* section
  of the README for what is measured and what is not.
- The experimental polar decoder follows the polar code construction of Arıkan
  and the CRC-aided successive-cancellation list decoder of Tal and Vardy; the
  ordered-statistics reprocessing follows Fossorier and Lin. These are
  algorithm descriptions, not code, and none is taken from an implementation.
- The convolutional coding and Viterbi decoding, the BCH construction of Bose,
  Chaudhuri and Hocquenghem, Chase's algorithm and Pyndiah's product-code
  decoding are likewise implemented here from their published descriptions.
  CRC-24/OPENPGP is used as specified for that CRC.

If something here is misattributed or missing, please open an issue and it will
be corrected.
