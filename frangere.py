#!/usr/bin/env python3


'''frangere - v. latin. to break

This script fragments an AV2 stream packaged in an MP4 into a fragmented MP4

Usage:
  # A basic conversion, defaults to av2E 4CC.
  ./frangere.py input.mp4 output.mp4

  # The 4CC can be configured
  ./frangere.py input.mp4 output.mp4 --fourcc av02

  # Strict keyframe splitting
  ./frangere.py input.mp4 output.mp4 --samples-per-fragment 0

  # Can be split into an init-segment and chunks for HLS
  ./frangere.py input.mp4 output.mp4 --split-dir ./segments
'''

import argparse
import dataclasses
import pathlib
import struct
import sys


assert sys.version_info >= (3, 14), 'update ya python, dummy'


def ParseArgs() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description='Remux & Fragment AV2 MP4s')

  parser.add_argument(
    'input',
    type=pathlib.Path,
    help='Path to unfragmented MP4 file with AV2 stream',
  )

  parser.add_argument(
    'output',
    nargs='?',
    type=pathlib.Path,
    default=None,
    help='Path to fragmented file. Defaults to <input>_frag.mp4',
  )

  parser.add_argument(
    '--fourcc',
    choices=['av2E', 'av02'],
    default='av2E',
    help='Sample entry 4CC to write in stsd (default av2E)',
  )

  parser.add_argument(
    '--samples-per-fragment',
    type=int,
    default=60,
    help='Max samples in a moof box. Use 0 to split at keyframes (default: 60)',
  )

  parser.add_argument(
    '--split-dir',
    type=pathlib.Path,
    default=None,
    help='[Optional] Directory to put init segment and chunks for HLS',
  )

  return parser.parse_args()


def box(fourcc: bytes, payload: bytes) -> bytes:
  assert len(fourcc) == 4
  return struct.pack('>I4s', 8 + len(payload), fourcc) + payload


def full_box(fourcc: bytes, version: int, flags: int, payload: bytes) -> bytes:
  vf = ((version & 0xFF) << 24) | (flags & 0xFFFFFF)
  return box(fourcc, struct.pack('>I', vf) + payload)


@dataclasses.dataclass
class ParsedMP4:
  timescale: int
  duration: int
  width: int
  height: int
  vse_prefix: bytes
  orig_4cc: bytes
  extra_child_boxes: dict[bytes, bytes]
  config_obus: bytes
  durations: list[int]
  cts_offsets: list[int]
  sizes: list[int]
  samples: list[bytes]
  sync_set_1based: set[int]

  @dataclasses.dataclass
  class VSEData:
    _4CC: bytes
    prefix: bytes
    width: int
    height: int
    extra_boxes: dict[bytes, bytes]

  @staticmethod
  def IterateBoxes(buffer: bytes, start: int = 0, end: int | None = None):
    if end is None:
      end = len(buffer)
    pos = start
    while pos + 8 <= end:
      size, btype = struct.unpack_from('>I4s', buffer, pos)
      hdr = 8
      if size == 1:
        size = struct.unpack_from('>Q', buffer, pos + 8)[0]
        hdr = 16
      elif size == 0:
        size = end - pos
      if size < hdr:
        raise ValueError(f'invalid box {size=}@{pos=}')
      yield btype, buffer[pos + hdr : pos + size], pos, size
      pos += size

  @classmethod
  def FindBox(cls, buffer: bytes, *path: bytes) -> bytes | None:
    current = buffer
    for step in path:
      found = False
      for btype, payload, _, _ in cls.IterateBoxes(current):
        if btype == step:
          current = payload
          found = True
          break
      if not found:
        return None
    return current

  @classmethod
  def Parse(cls, mp4_bytes: bytes) -> ParsedMP4:
    if (moov := cls.FindBox(mp4_bytes, b'moov')) is None:
      raise ValueError('no moov')
    if (video_mdia := cls._find_video_mdia(moov)) is None:
      raise ValueError('no vide in moov')
    if (stbl := cls.FindBox(video_mdia, b'minf', b'stbl')) is None:
      raise ValueError('no minf/stbl in vide')
    timescale = cls._parse_timescale(video_mdia)
    vse_data, config_obus = cls._parse_stsd(stbl)
    durations = cls._parse_stts(stbl)
    num_samples = len(durations)
    cts_offsets = cls._parse_ctts(stbl, num_samples)
    sizes = cls._parse_stsz(stbl, num_samples)
    chunk_offsets = cls._parse_chunk_offsets(stbl)
    samples = cls._extract_samples(mp4_bytes, stbl, chunk_offsets, sizes, num_samples)
    sync_set = cls._parse_stss(stbl, num_samples)

    return cls(
      timescale=timescale,
      duration=sum(durations),
      width=vse_data.width,
      height=vse_data.height,
      vse_prefix=vse_data.prefix,
      orig_4cc=vse_data._4CC,
      extra_child_boxes=vse_data.extra_boxes,
      config_obus=config_obus,
      durations=durations,
      cts_offsets=cts_offsets,
      sizes=sizes,
      samples=samples,
      sync_set_1based=sync_set,
    )

  @classmethod
  def _find_video_mdia(cls, moov: bytes) -> bytes | None:
    for btype, trak_payload, _, _ in cls.IterateBoxes(moov):
      if btype != b'trak':
        continue
      hdlr = cls.FindBox(trak_payload, b'mdia', b'hdlr')
      if hdlr and len(hdlr) >= 12 and hdlr[8:12] == b'vide':
        return cls.FindBox(trak_payload, b'mdia')
    return None

  @classmethod
  def _parse_timescale(cls, video_mdia: bytes) -> int:
    if (mdhd := cls.FindBox(video_mdia, b'mdhd')) is None:
      raise ValueError('missing mdhd box')
    if mdhd[0] == 1:
      timescale = struct.unpack_from('>IQ', mdhd, 20)[0]
    else:
      timescale = struct.unpack_from('>II', mdhd, 12)[0]
    if not timescale:
      raise ValueError('invalid timescale 0 in mdhd')
    return timescale

  @classmethod
  def _extract_config_obus_from_av2c(cls, av2c_payload: bytes) -> bytes:
    # get raw length delimited config_obus from v1 av2_mux or av2-isobmff
    # OutOfBandAV2CodecConfigurationRecord:
    #   uint8 reserved1 = 0;
    #   uint8 config_obus_count_minus1;
    #   config_obu()...
    if len(av2c_payload) >= 3 and av2c_payload[0] == 0:
      return av2c_payload[2:]
    # Legacy AV2CodecConfigurationRecord:
    # uint8 marker(1)=1 | version(7)=1 -> 0x81 or byte[0] == 1
    if len(av2c_payload) >= 11 and av2c_payload[0] in (1, 0x81):
      return av2c_payload[9:]
    raise ValueError('bad av2C box')

  @classmethod
  def _parse_stsd(cls, stbl: bytes) -> tuple[ParsedMP4.VSEData, bytes]:
    stsd = cls.FindBox(stbl, b'stsd')
    if stsd is None or struct.unpack_from('>I', stsd, 4)[0] < 1:
      raise ValueError('no stsd')

    sample_entry_4cc, sample_entry_payload, _, _ = next(cls.IterateBoxes(stsd, 8))
    if len(sample_entry_payload) < 78:
      raise ValueError('payload too short ( < 78 bytes )')

    width, height = struct.unpack_from('>HH', sample_entry_payload, 24)

    av2c_payload = None
    extra_child_boxes = {}
    for ctype, cpayload, cpos, csize in cls.IterateBoxes(sample_entry_payload, 78):
      if ctype == b'av2C':
        av2c_payload = cpayload
      else:
        extra_child_boxes[ctype] = sample_entry_payload[cpos : cpos + csize]

    if av2c_payload is None:
      raise ValueError('no av2C in VSE')

    config_obus = cls._extract_config_obus_from_av2c(av2c_payload)

    vse_data = cls.VSEData(
      _4CC=sample_entry_4cc,
      prefix=sample_entry_payload[:78],
      width=width,
      height=height,
      extra_boxes=extra_child_boxes,
    )
    return vse_data, config_obus

  @classmethod
  def _parse_stts(cls, stbl: bytes) -> list[int]:
    if (stts := cls.FindBox(stbl, b'stts')) is None:
      raise ValueError('no stts')
    stts_count = struct.unpack_from('>I', stts, 4)[0]
    durations = []
    off = 8
    for _ in range(stts_count):
      cnt, delta = struct.unpack_from('>II', stts, off)
      durations.extend([delta] * cnt)
      off += 8
    return durations

  @classmethod
  def _parse_ctts(cls, stbl: bytes, num_samples: int) -> list[int]:
    cts_offsets = [0] * num_samples
    if (ctts := cls.FindBox(stbl, b'ctts')) is None:
      return cts_offsets

    ctts_ver = ctts[0]
    ctts_count = struct.unpack_from('>I', ctts, 4)[0]
    raw_offsets = []
    off = 8
    fmt = '>Ii' if ctts_ver == 1 else '>II'

    for _ in range(ctts_count):
      cnt, coff = struct.unpack_from(fmt, ctts, off)
      raw_offsets.extend([coff] * cnt)
      off += 8

    for i in range(min(num_samples, len(raw_offsets))):
      cts_offsets[i] = raw_offsets[i]
    return cts_offsets

  @classmethod
  def _parse_stsz(cls, stbl: bytes, num_samples: int) -> list[int]:
    if (stsz := cls.FindBox(stbl, b'stsz')) is None:
      raise ValueError('no stsz')

    default_size, sample_count = struct.unpack_from('>II', stsz, 4)
    if sample_count != num_samples:
      raise ValueError(f'stsz (#{sample_count}) != stts ({num_samples})')

    if default_size != 0:
      return [default_size] * sample_count
    return list(struct.unpack_from(f'>{sample_count}I', stsz, 12))

  @classmethod
  def _parse_chunk_offsets(cls, stbl: bytes) -> list[int]:
    if stco := cls.FindBox(stbl, b'stco'):
      chunk_count = struct.unpack_from('>I', stco, 4)[0]
      return list(struct.unpack_from(f'>{chunk_count}I', stco, 8))

    if co64 := cls.FindBox(stbl, b'co64'):
      chunk_count = struct.unpack_from('>I', co64, 4)[0]
      return list(struct.unpack_from(f'>{chunk_count}Q', co64, 8))

    raise ValueError('no stco/co64')

  @classmethod
  def _extract_samples(cls, mp4: bytes, stbl: bytes, offsets: list[int], sizes: list[int], samples: int) -> list[bytes]:
    stsc = cls.FindBox(stbl, b'stsc')
    if stsc is None:
      raise ValueError('missing stsc')

    stsc_count = struct.unpack_from('>I', stsc, 4)[0]
    if stsc_count < 1 and samples > 0:
      raise ValueError('empty stsc')
    stsc_entries = []
    off = 8
    for _ in range(stsc_count):
      first_chunk, samples_per_chunk, sdi = struct.unpack_from('>III', stsc, off)
      stsc_entries.append((first_chunk, samples_per_chunk, sdi))
      off += 12

    sample_blobs = []
    sample_idx = 0
    for chunk_idx_0, chunk_file_offset in enumerate(offsets):
      chunk_num_1 = chunk_idx_0 + 1
      spc = stsc_entries[0][1]

      for first_chunk, entry_spc, _ in stsc_entries:
        if chunk_num_1 >= first_chunk:
          spc = entry_spc
        else:
          break

      cur_off = chunk_file_offset
      for _ in range(spc):
        if sample_idx >= samples:
          break
        sz = sizes[sample_idx]
        sample_blobs.append(mp4[cur_off : cur_off + sz])
        cur_off += sz
        sample_idx += 1

    if len(sample_blobs) != samples:
      raise ValueError(f'{len(sample_blobs)} samples, expected {samples}')

    return sample_blobs

  @classmethod
  def _parse_stss(cls, stbl: bytes, num_samples: int) -> set[int]:
    stss = cls.FindBox(stbl, b'stss')
    if stss is None:
      return set(range(1, num_samples + 1))
    sync_count = struct.unpack_from('>I', stss, 4)[0]
    return set(struct.unpack_from(f'>{sync_count}I', stss, 8))


class InitSegment:
  UNITY_MX = struct.pack('>9I', 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)

  @staticmethod
  def _count_length_delim_obus(buf: bytes) -> int:
    pos = 0
    count = 0
    while pos < len(buf):
      obu_len = 0
      for i in range(8):
        if pos >= len(buf):
          raise ValueError('Truncated leb128 in config_obus')
        b = buf[pos]
        pos += 1
        obu_len |= (b & 0x7F) << (7 * i)
        if (b & 0x80) == 0:
          break
      else:
        raise ValueError('Unterminated leb128 in config_obus')
      pos += obu_len
      count += 1
    if pos != len(buf) or not (1 <= count <= 256):
      raise ValueError(f'Malformed length-delimited config_obus (ended at {pos}/{len(buf)})')
    return count

  @staticmethod
  def _build_ftyp(sample_entry_4cc: bytes) -> bytes:
    brands = [b'isom', b'iso6', b'av02', b'mp41']
    if sample_entry_4cc not in brands:
      brands.insert(2, sample_entry_4cc)
    return box(b'ftyp', b'isom' + struct.pack('>I', 0x200) + b''.join(brands))

  @classmethod
  def _build_mvhd(cls, timescale: int) -> bytes:
    # fragMP4 init segs set mvhd/tkhd/mdhd duration to 0 and carry
    # the total movie duration in moov/mvex/mehd.
    payload = (
      struct.pack('>IIII', 0, 0, timescale, 0)
      + struct.pack('>IH', 0x00010000, 0x0100)
      + b'\x00' * 10
      + cls.UNITY_MX
      + b'\x00' * 24
      + struct.pack('>I', 2)  # next_track_ID
    )
    return full_box(b'mvhd', 0, 0, payload)

  @classmethod
  def _build_tkhd(cls, width: int, height: int) -> bytes:
    # put w/h in fp16 into tkhd. av2_mux fucks up the tkhd and makes it always 320x240?
    payload = (
      struct.pack('>III', 0, 0, 1)  # creation, mod, track_ID=1
      + struct.pack('>II', 0, 0)     # reserved, duration=0
      + b'\x00' * 8
      + struct.pack('>HHHH', 0, 0, 0, 0)
      + cls.UNITY_MX
      + struct.pack('>II', width << 16, height << 16)
    )
    return full_box(b'tkhd', 0, 0x000007, payload)

  @classmethod
  def _build_stbl(cls, mp4: ParsedMP4, sample_entry_4cc: bytes) -> bytes:
    obu_count = cls._count_length_delim_obus(mp4.config_obus)
    # av2isobmff OutOfBandAV2CodecConfigurationBox ('av2C').
    av2c = box(b'av2C', bytes([0x00, obu_count - 1]) + mp4.config_obus)

    extra = dict(mp4.extra_child_boxes)
    colr = extra.pop(b'colr', box(b'colr', b'nclx' + struct.pack('>HHHB', 1, 1, 1, 0x00)))
    pasp = extra.pop(b'pasp', box(b'pasp', struct.pack('>II', 1, 1)))
    remaining_children = b''.join(extra.values())

    sample_entry = box(sample_entry_4cc, mp4.vse_prefix + av2c + colr + pasp + remaining_children)
    stsd = full_box(b'stsd', 0, 0, struct.pack('>I', 1) + sample_entry)
    stts = full_box(b'stts', 0, 0, struct.pack('>I', 0))
    stsc = full_box(b'stsc', 0, 0, struct.pack('>I', 0))
    stsz = full_box(b'stsz', 0, 0, struct.pack('>II', 0, 0))
    stco = full_box(b'stco', 0, 0, struct.pack('>I', 0))
    return box(b'stbl', stsd + stts + stsc + stsz + stco)

  @classmethod
  def _build_trak(cls, mp4: ParsedMP4, sample_entry_4cc: bytes) -> bytes:
    tkhd = cls._build_tkhd(mp4.width, mp4.height)
    mdhd = full_box(b'mdhd', 0, 0, struct.pack('>IIIIHH', 0, 0, mp4.timescale, 0, 0x55C4, 0))
    hdlr = full_box(b'hdlr', 0, 0, struct.pack('>I4s3I', 0, b'vide', 0, 0, 0) + b'AV2 VideoHandler\x00')
    vmhd = full_box(b'vmhd', 0, 1, struct.pack('>4H', 0, 0, 0, 0))
    dref = full_box(b'dref', 0, 0, struct.pack('>I', 1) + full_box(b'url ', 0, 1, b''))
    dinf = box(b'dinf', dref)
    stbl = cls._build_stbl(mp4, sample_entry_4cc)

    minf = box(b'minf', vmhd + dinf + stbl)
    mdia = box(b'mdia', mdhd + hdlr + minf)
    return box(b'trak', tkhd + mdia)

  @staticmethod
  def _build_mvex(total_duration: int) -> bytes:
    if total_duration <= 0xFFFFFFFF:
      mehd = full_box(b'mehd', 0, 0, struct.pack('>I', total_duration))
    else:
      mehd = full_box(b'mehd', 1, 0, struct.pack('>Q', total_duration))
    # default_sample_flags = 0x01010000
    #   sample_depends_on=1
    #   is_non_sync=1
    trex = full_box(b'trex', 0, 0, struct.pack('>5I', 1, 1, 1000, 0, 0x01010000))
    return box(b'mvex', mehd + trex)

  @classmethod
  def build(cls, mp4: ParsedMP4, sample_entry_4cc: bytes) -> bytes:
    ftyp = cls._build_ftyp(sample_entry_4cc)
    mvhd = cls._build_mvhd(mp4.timescale)
    trak = cls._build_trak(mp4, sample_entry_4cc)
    mvex = cls._build_mvex(mp4.duration)
    moov = box(b'moov', mvhd + trak + mvex)
    return ftyp + moov


@dataclasses.dataclass
class FragmentMetadata:
  seq: int
  sampleStart: int
  sampleEnd: int
  sampleCount: int
  dtsStartSec: float
  dtsEndSec: float
  ptsStartSec: float
  ptsEndSec: float
  byteLength: int
  hasKeyframe: bool

  @dataclasses.dataclass
  class SampleFormat:
    trun_entries: bytearray
    mdat_payload: bytearray
    min_pts: int | None
    max_pts: int | None
    cur_dts: int
    has_keyframe: bool

  @classmethod
  def _frag_ranges(cls, samplez:int, sync_set: set[int], spf: int) -> list[tuple[int, int]]:
    if samplez == 0:
      return []
    if spf <= 0:
      # split at sync sampl / stss keyframe boundaries.
      cuts = sorted(idx - 1 for idx in sync_set if 1 < idx <= samplez)
      starts = [0] + cuts
      ends = cuts + [samplez]
      return list(zip(starts, ends))
    return [(start, min(samplez, start + spf)) for start in range(0, samplez, spf)]

  @classmethod
  def build_fragments(cls, mp4: ParsedMP4, spf: int = 60) -> list[tuple[bytes, FragmentMetadata]]:
    num_samples = len(mp4.samples)
    fragments = []
    base_dts = 0
    seq_num = 1

    ranges = cls._frag_ranges(num_samples, mp4.sync_set_1based, spf)

    for start, end in ranges:
      count = end - start

      mfhd = full_box(b'mfhd', 0, 0, struct.pack('>I', seq_num))
      # tfhd flags:
      # 0x020000 = default-base-is-moof (ISO/IEC 14496-12iso6 / MSE requirement)
      # 0x000020 = default-sample-flags-present
      tfhd = full_box(b'tfhd', 0, 0x020020, struct.pack('>II', 1, 0x01010000))
      tfdt = full_box(b'tfdt', 1, 0, struct.pack('>Q', base_dts))
      fmt = cls._process_samples(mp4, start, end, base_dts)

      trun_flags = 0x000F01
      dummy_moof = cls._create_moof(mfhd, tfhd, tfdt, count, 0, fmt.trun_entries, trun_flags)
      data_offset = len(dummy_moof) + 8  # +8 bytes for mdat header

      moof = cls._create_moof(mfhd, tfhd, tfdt, count, data_offset, fmt.trun_entries, trun_flags)
      assert len(moof) == len(dummy_moof)
      mdat = box(b'mdat', bytes(fmt.mdat_payload))
      frag_bytes = moof + mdat
      fragments.append((frag_bytes, cls(
        seq=seq_num,
        sampleStart=start,
        sampleEnd=end - 1,
        sampleCount=count,
        dtsStartSec=round(base_dts / mp4.timescale, 3),
        dtsEndSec=round(fmt.cur_dts / mp4.timescale, 3),
        ptsStartSec=round((fmt.min_pts or 0) / mp4.timescale, 3),
        ptsEndSec=round((fmt.max_pts or 0) / mp4.timescale, 3),
        byteLength=len(frag_bytes),
        hasKeyframe=fmt.has_keyframe,
      )))
      base_dts = fmt.cur_dts
      seq_num += 1

    return fragments

  @classmethod
  def _process_samples(cls, mp4: ParsedMP4, start: int, end: int, base_dts: int) -> FragmentMetadata.SampleFormat:
    trun_entries = bytearray()
    mdat_payload = bytearray()
    min_pts = None
    max_pts = None
    has_keyframe = False
    cur_dts = base_dts

    for idx in range(start, end):
      is_sync = (idx + 1) in mp4.sync_set_1based
      if is_sync:
        has_keyframe = True

      # iso 14496-12 sample flags:
      # 0x02000000 -> sample_depends_on=2 (independent / keyframe), non_sync=0
      # 0x01010000 -> sample_depends_on=1 (inter frame), non_sync=1
      sflags = 0x02000000 if is_sync else 0x01010000
      dur = mp4.durations[idx]
      sz = mp4.sizes[idx]
      coff = mp4.cts_offsets[idx]
      pts = cur_dts + coff

      min_pts = pts if min_pts is None else min(min_pts, pts)
      max_pts = (pts + dur) if max_pts is None else max(max_pts, pts + dur)

      trun_entries += struct.pack('>IIIi', dur, sz, sflags, coff)
      mdat_payload += mp4.samples[idx]
      cur_dts += dur

    return cls.SampleFormat(trun_entries, mdat_payload, min_pts, max_pts, cur_dts, has_keyframe)

  @staticmethod
  def _create_moof(mfhd: bytes, tfhd: bytes, tfdt: bytes, count: int, offset: int, entries: bytes | bytearray, flags: int) -> bytes:
    # trun flags: 0x000F01 = offset (0x001)
    #                      | sample_duration (0x100)
    #                      | sample_size (0x200)
    #                      | sample_flags (0x400)
    #                      | sample_composition_time_offset (0x800)
    packed = struct.pack('>Ii', count, offset) + entries
    trun = full_box(b'trun', 1, flags, packed)
    return box(b'moof', mfhd + box(b'traf', tfhd + tfdt + trun))


def main() -> int:
  args = ParseArgs()

  src_path: pathlib.Path = args.input
  mp4 = ParsedMP4.Parse(src_path.read_bytes())
  fragments = FragmentMetadata.build_fragments(mp4, args.samples_per_fragment)
  concat = b''.join(b for b, _ in fragments)

  out_path = args.output or src_path.with_name(f'{src_path.stem}_frag.mp4')
  fourcc = args.fourcc.encode('ascii')
  init_bytes = InitSegment.build(mp4, fourcc)

  out_path.parent.mkdir(parents=True, exist_ok=True)
  out_path.write_bytes(init_bytes + concat)

  if args.split_dir is not None:
    args.split_dir.mkdir(parents=True, exist_ok=True)
    (args.split_dir / 'init.mp4').write_bytes(init_bytes)
    for fb, meta in fragments:
      seg_name = f'seg_{meta.seq:03d}.m4s'
      (args.split_dir / seg_name).write_bytes(fb)
    print(f'init.mp4 & {len(fragments)} .m4s segments now in {args.split_dir}')

  return 0


if __name__ == '__main__':
  raise SystemExit(main())
