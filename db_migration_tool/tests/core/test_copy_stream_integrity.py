"""CopyStreamBuffer가 원본 COPY OUT 데이터를 한 바이트도 잃지 않고, 재개 키를 정확히 뽑는지.

감사 문서(docs/plans/code-audit-remediation-2026-08-26.md)의 두 결함을 고정한다.

- C-01: 큐가 가득 찬 순간 생산자가 close()하면 종료 신호 대신 취소가 걸려, 소비자가 큐에
  남은 청크를 버리고 EOF를 돌려줬다. 대상에는 앞부분만 들어가는데 행 수·마지막 키는 생산자
  기준이라 checkpoint가 전진했다 — 조용한 누락.
- C-02: COPY CSV를 split("\\n")/split(",")로 읽어 따옴표 안의 줄바꿈·쉼표를 행/필드 경계로
  오인했다. 행 수가 부풀고 재개 키가 값 조각으로 바뀔 수 있다.
"""

from __future__ import annotations

import random
import threading
import time

import pytest

from src.core.copy_migration_worker import CopyStreamBuffer


def _drain(buffer: CopyStreamBuffer, size: int = 7, delay: float = 0.0) -> str:
    """대상 COPY FROM처럼 read(size)를 EOF("")까지 반복한다."""
    out: list[str] = []
    while True:
        chunk = buffer.read(size)
        if chunk == "":
            return "".join(out)
        out.append(chunk)
        if delay:
            time.sleep(delay)


def _produce(buffer: CopyStreamBuffer, chunks: list[str]) -> threading.Thread:
    """원본 COPY OUT처럼 write()를 호출한 뒤 close()한다(워커의 copy_out과 같은 순서)."""

    def run() -> None:
        try:
            for c in chunks:
                buffer.write(c)
        finally:
            buffer.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _rows(n: int) -> list[str]:
    return [f"{1000 + i},{170000 + i},{i}.5,t\n" for i in range(n)]


class TestNoDataLossOnClose:
    """C-01"""

    @pytest.mark.parametrize("queue_size", [1, 2, 8])
    def test_slow_consumer_receives_every_chunk(self, queue_size):
        """소비자가 느려 큐가 가득 찬 채로 close()돼도 모든 데이터가 전달돼야 한다."""
        chunks = _rows(40)
        buffer = CopyStreamBuffer(max_queue_size=queue_size)
        producer = _produce(buffer, chunks)

        received = _drain(buffer, size=5, delay=0.002)
        producer.join(timeout=5)

        assert received == "".join(chunks)
        assert buffer.row_count == 40
        assert buffer.last_key == "1039"

    def test_close_is_idempotent(self):
        """set_error()가 close()를 부르고 워커 finally가 또 부른다. 두 번째 호출이 막히면 안 된다."""
        buffer = CopyStreamBuffer(max_queue_size=1)
        buffer.write("1,2,3,t\n")
        consumer = threading.Thread(target=_drain, args=(buffer,), daemon=True)
        consumer.start()
        buffer.close()
        buffer.close()
        consumer.join(timeout=5)
        assert not consumer.is_alive()

    def test_consumed_equals_produced_after_eof(self):
        """커밋 전에 워커가 부르는 완전성 확인: 생산한 만큼 소비됐는가."""
        chunks = _rows(10)
        buffer = CopyStreamBuffer(max_queue_size=1)
        producer = _produce(buffer, chunks)
        _drain(buffer, delay=0.001)
        producer.join(timeout=5)

        buffer.assert_fully_consumed()  # 예외 없이 통과

    def test_incomplete_consumption_is_detected(self):
        """소비자가 중간에 멈추면 완전성 확인이 실패해야 한다(커밋 금지)."""
        buffer = CopyStreamBuffer(max_queue_size=8)
        for c in _rows(5):
            buffer.write(c)
        buffer.read(10)  # 일부만 소비

        with pytest.raises(RuntimeError):
            buffer.assert_fully_consumed()


class TestCancelAndErrorAbortTheCopy:
    """취소·오류는 '짧은 EOF'가 아니라 예외여야 대상 COPY가 실패하고 커밋되지 않는다."""

    def test_read_after_cancel_raises(self):
        buffer = CopyStreamBuffer(max_queue_size=2)
        buffer.write("1,2,3,t\n")
        buffer.cancel()
        with pytest.raises(RuntimeError):
            buffer.read(100)

    def test_producer_error_reaches_consumer(self):
        buffer = CopyStreamBuffer(max_queue_size=2)
        buffer.write("1,2,3,t\n")
        buffer.set_error(ValueError("source failed"))
        with pytest.raises(ValueError):
            _drain(buffer)


class TestCsvRecordTracking:
    """C-02 — PostgreSQL CSV 규칙(따옴표 안 줄바꿈·쉼표·"" 이스케이프)과 청크 경계."""

    TRICKY = (
        '1001,1700000000001,"a\nb",t\n'  # 따옴표 안 줄바꿈
        '1002,1700000000002,"x,y",f\n'  # 따옴표 안 쉼표
        '1003,1700000000003,"say ""hi""\n2,3",t\n'  # 이스케이프된 따옴표 + 줄바꿈 + 숫자처럼 보이는 조각
        "1004,1700000000004,NULL,t\n"
    )

    def _feed_in_pieces(self, text: str, cuts: list[int]) -> CopyStreamBuffer:
        buffer = CopyStreamBuffer(max_queue_size=1000)
        pieces, prev = [], 0
        for c in sorted(set(cuts)) + [len(text)]:
            pieces.append(text[prev:c])
            prev = c
        producer = _produce(buffer, [p for p in pieces if p])
        _drain(buffer, size=3)
        producer.join(timeout=5)
        return buffer

    def test_quoted_newlines_and_commas_are_not_boundaries(self):
        buffer = self._feed_in_pieces(self.TRICKY, [])
        assert buffer.row_count == 4
        assert buffer.last_key == "1004"
        assert buffer.last_date == "1700000000004"

    def test_every_single_split_point(self):
        """모든 위치에서 청크가 잘려도 결과가 같아야 한다."""
        for cut in range(1, len(self.TRICKY)):
            buffer = self._feed_in_pieces(self.TRICKY, [cut])
            assert (buffer.row_count, buffer.last_key, buffer.last_date) == (
                4,
                "1004",
                "1700000000004",
            ), f"cut={cut}"

    def test_random_chunking(self):
        rng = random.Random(20260925)
        for _ in range(200):
            cuts = [rng.randrange(1, len(self.TRICKY)) for _ in range(rng.randrange(1, 12))]
            buffer = self._feed_in_pieces(self.TRICKY, cuts)
            assert (buffer.row_count, buffer.last_key) == (4, "1004"), cuts

    def test_last_row_with_multiline_value_keeps_real_key(self):
        """배치 마지막 행의 값에 줄바꿈이 있으면 예전 코드는 값 조각('2')을 키로 기록했다."""
        text = '1005,1700000000005,"tail\n2,3",t\n'
        buffer = self._feed_in_pieces(text, [20])
        assert buffer.row_count == 1
        assert buffer.last_key == "1005"
        assert buffer.last_date == "1700000000005"

    def test_final_record_without_trailing_newline(self):
        buffer = self._feed_in_pieces("1,10,a,t\n2,20,b,f", [])
        assert buffer.row_count == 2
        assert buffer.last_key == "2"

    def test_extra_pk_columns_are_tracked(self):
        """RUNNING_TIME_HISTORY: (path_id, issued_date, save_type)."""
        buffer = CopyStreamBuffer(max_queue_size=100, extra_track_indices=[2])
        producer = _produce(buffer, ['7,70,1,"x,y"\n', "8,80,2,z\n"])
        _drain(buffer)
        producer.join(timeout=5)
        assert buffer.last_extra == {2: "2"}

    def test_unterminated_quote_at_eof_is_an_error(self):
        buffer = CopyStreamBuffer(max_queue_size=10)
        producer = _produce(buffer, ['1,10,"never closed\n'])
        with pytest.raises(RuntimeError):
            _drain(buffer)
        producer.join(timeout=5)
