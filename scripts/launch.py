#!/usr/bin/env python3
"""OCI Always Free 자동 재시도 생성 스크립트.

cron으로 주기적으로 실행되어 VM.Standard.A1.Flex (ARM Ampere)
인스턴스 생성을 시도한다. 자원 부족("Out of host capacity")으로
실패하면 exit 0으로 다음 cron을 기다린다. 성공하면 state.json을 갱신하고
디스코드로 알린 뒤 exit 0.

모든 OCID·인증 정보·설정값은 환경변수로 받는다 (하드코딩 금지).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# 로그 (print는 쓰지 않음)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("oci-retry")


# ---------------------------------------------------------------------------
# 환경변수 (없으면 바로 에러 종료)
# ---------------------------------------------------------------------------
def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error("필수 환경변수 %s 가 비어 있어요. .env를 확인하세요.", name)
        sys.exit(1)
    return value


def _optional_env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


# ---------------------------------------------------------------------------
# state.json 읽기·쓰기
# ---------------------------------------------------------------------------
def state_path() -> Path:
    """state.json 경로 (작업 폴더 기준)."""
    return Path.cwd() / "state.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        logger.info("state.json이 없어요. 첫 실행으로 보고 계속해요.")
        return _fresh_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("state.json을 읽지 못했어요 (%s). 처음 상태로 계속해요.", exc)
        return _fresh_state()
    if not isinstance(data, dict):
        logger.warning("state.json 형식이 잘못됐어요. 처음 상태로 계속해요.")
        return _fresh_state()
    return data


def _fresh_state() -> dict[str, Any]:
    return {
        "success": False,
        "instance_ocid": None,
        "public_ip": None,
        "created_at": None,
        "last_attempt_at": None,
        "last_error": None,
        "attempt_count": 0,
    }


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("state.json 저장: %s", path)


# ---------------------------------------------------------------------------
# 디스코드 알림
# ---------------------------------------------------------------------------
def discord_notify(embed: dict[str, Any]) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.info("DISCORD_WEBHOOK_URL이 없어서 알림을 건너뛰어요.")
        return
    payload = {"embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.status_code >= 400:
            logger.warning("디스코드 알림 실패 (status=%s body=%s)", resp.status_code, resp.text[:200])
        else:
            logger.info("디스코드 알림 보냄")
    except requests.RequestException as exc:
        logger.warning("디스코드 알림 중 예외: %s", exc)


def notify_success(
    display_name: str,
    instance_ocid: str,
    public_ip: str | None,
    region: str,
    ad: str,
    ocpus: str,
    memory_gb: str,
    created_at: str,
    already_exists: bool,
) -> None:
    title = "✅ 이미 인스턴스가 있어요" if already_exists else "🎉 OCI 서버 생성 성공!"
    embed = {
        "title": title,
        "color": 0x00B050 if already_exists else 0x57F287,
        "fields": [
            {"name": "display_name", "value": display_name, "inline": False},
            {"name": "instance_ocid", "value": instance_ocid, "inline": False},
            {"name": "public_ip", "value": public_ip or "(못 가져옴)", "inline": True},
            {"name": "region", "value": region, "inline": True},
            {"name": "availability_domain", "value": ad, "inline": True},
            {"name": "ocpus", "value": ocpus, "inline": True},
            {"name": "memory_gb", "value": memory_gb, "inline": True},
            {"name": "created_at", "value": created_at, "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    discord_notify(embed)


def notify_error(code: str, message: str, status: str | int) -> None:
    embed = {
        "title": "❌ OCI 생성 에러",
        "color": 0xED4245,
        "fields": [
            {"name": "code", "value": code, "inline": True},
            {"name": "status", "value": str(status), "inline": True},
            {"name": "message", "value": message[:1000], "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    discord_notify(embed)


# ---------------------------------------------------------------------------
# OCI 클라이언트
# ---------------------------------------------------------------------------
def build_oci_config() -> dict[str, Any]:
    tenancy = _require_env("OCI_TENANCY_OCID")
    user = _require_env("OCI_USER_OCID")
    fingerprint = _require_env("OCI_API_KEY_FINGERPRINT")
    region = _require_env("OCI_REGION")
    key_content = os.environ.get("OCI_API_KEY_CONTENT")
    key_file = os.environ.get("OCI_API_KEY_FILE")

    config: dict[str, Any] = {
        "user": user,
        "tenancy": tenancy,
        "fingerprint": fingerprint,
        "region": region,
    }

    if key_content:
        # PEM 내용을 임시 파일로 (CI에서는 내용을 Secret으로 받음)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False, encoding="utf-8")
        tmp.write(key_content)
        tmp.flush()
        tmp.close()
        os.chmod(tmp.name, 0o600)
        config["key_file"] = tmp.name
        logger.info("OCI API 키를 임시 파일로 저장: %s", tmp.name)
    elif key_file:
        if not Path(key_file).exists():
            logger.error("OCI_API_KEY_FILE 파일이 없어요: %s", key_file)
            sys.exit(1)
        config["key_file"] = key_file
    else:
        logger.error("OCI_API_KEY_CONTENT 또는 OCI_API_KEY_FILE 중 하나를 설정하세요.")
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def main() -> int:
    # 설정값 (전부 환경변수)
    tenancy_ocid = _require_env("OCI_TENANCY_OCID")
    compartment_ocid = _require_env("OCI_COMPARTMENT_OCID")
    region = _require_env("OCI_REGION")
    subnet_ocid = _require_env("OCI_SUBNET_OCID")
    ssh_public_key = _require_env("OCI_SSH_PUBLIC_KEY")

    shape = _optional_env("OCI_SHAPE", "VM.Standard.A1.Flex")
    ocpus = _optional_env("OCI_OCPUS", "2")
    memory_gb = _optional_env("OCI_MEMORY_GB", "12")
    display_name = _optional_env("OCI_DISPLAY_NAME", "auto-retry-freenium")
    os_name = _optional_env("OCI_OS", "Canonical Ubuntu")
    os_version = _optional_env("OCI_OS_VERSION", "24.04")
    arch = _optional_env("OCI_ARCH", "ARM")
    boot_volume_size_gb = int(_optional_env("OCI_BOOT_VOLUME_SIZE_GB", "50"))

    # state.json 읽기
    state = load_state()

    # 1. 이미 성공했으면 건너뜀
    if state.get("success") is True:
        logger.info("이미 성공한 기록(success=true)이 있어서 건너뛰어요.")
        return 0

    # 429로 쉬는 중이면 요청을 안 보냄
    wait = float(state.get("backoff_until") or 0) - time.time()
    if wait > 0:
        logger.info("429 때문에 쉬는 중이에요 (%d초 남음).", wait)
        return 0

    # OCI SDK import (환경변수 확인을 먼저 하려고 늦게 import)
    try:
        import oci
        from oci.core.models import (  # type: ignore
            CreateVnicDetails,
            InstanceSourceViaImageDetails,
            LaunchInstanceDetails,
            LaunchInstanceShapeConfigDetails,
        )
        from oci.exceptions import ServiceError
    except ImportError as exc:
        logger.error("oci SDK를 불러오지 못했어요: %s", exc)
        return 1

    config = build_oci_config()
    identity_client = oci.identity.IdentityClient(config)
    compute_client = oci.core.ComputeClient(config)
    network_client = oci.core.VirtualNetworkClient(config)

    state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    state["attempt_count"] = int(state.get("attempt_count", 0)) + 1

    # 2. 같은 이름의 인스턴스가 이미 있는지 (PROVISIONING/RUNNING/STARTING)
    try:
        existing = _find_existing_instance(compute_client, compartment_ocid, display_name)
    except ServiceError as exc:
        logger.error("기존 인스턴스 확인 중 ServiceError: code=%s status=%s message=%s", exc.code, exc.status, exc.message)
        state["last_error"] = f"{exc.code}: {exc.message}"
        save_state(state)
        notify_error(exc.code, exc.message, exc.status)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("기존 인스턴스 확인 중 예외: %s", exc)
        state["last_error"] = str(exc)
        save_state(state)
        return 1

    if existing is not None:
        logger.info("이미 인스턴스가 있어요 (id=%s state=%s).", existing.id, existing.lifecycle_state)
        public_ip = _get_public_ip(compute_client, network_client, compartment_ocid, existing.id)
        now = datetime.now(timezone.utc).isoformat()
        state.update(
            {
                "success": True,
                "instance_ocid": existing.id,
                "public_ip": public_ip,
                "created_at": existing.time_created.isoformat() if existing.time_created else now,
                "last_error": None,
            }
        )
        save_state(state)
        notify_success(
            display_name=display_name,
            instance_ocid=existing.id,
            public_ip=public_ip,
            region=region,
            ad=existing.availability_domain or "(unknown)",
            ocpus=ocpus,
            memory_gb=memory_gb,
            created_at=state["created_at"],
            already_exists=True,
        )
        return 0

    # 4. 이미지 OCID 찾기 (list_images로 최신 aarch64 이미지)
    try:
        image_id = _find_latest_image(
            compute_client,
            tenancy_ocid,
            os_name,
            os_version,
            shape,
            arch,
        )
    except ServiceError as exc:
        logger.error("이미지 찾기 중 ServiceError: code=%s status=%s message=%s", exc.code, exc.status, exc.message)
        state["last_error"] = f"{exc.code}: {exc.message}"
        save_state(state)
        notify_error(exc.code, exc.message, exc.status)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("이미지 찾기 중 예외: %s", exc)
        state["last_error"] = str(exc)
        save_state(state)
        return 1

    logger.info("사용할 이미지: %s", image_id)

    # 5. AD 목록
    try:
        ads = identity_client.list_availability_domains(compartment_id=tenancy_ocid).data
    except ServiceError as exc:
        logger.error("AD 목록 중 ServiceError: code=%s status=%s message=%s", exc.code, exc.status, exc.message)
        state["last_error"] = f"{exc.code}: {exc.message}"
        save_state(state)
        notify_error(exc.code, exc.message, exc.status)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("AD 목록 중 예외: %s", exc)
        state["last_error"] = str(exc)
        save_state(state)
        return 1

    if not ads:
        logger.error("Availability Domain을 하나도 못 가져왔어요.")
        state["last_error"] = "no availability domains"
        save_state(state)
        return 1

    logger.info("AD 목록: %s", [a.name for a in ads])

    # 6. AD마다 차례로 시도
    for ad in ads:
        ad_name = ad.name
        logger.info("%s 에 생성 시도", ad_name)
        launch_details = LaunchInstanceDetails(
            availability_domain=ad_name,
            compartment_id=compartment_ocid,
            display_name=display_name,
            shape=shape,
            shape_config=LaunchInstanceShapeConfigDetails(ocpus=float(ocpus), memory_in_gbs=float(memory_gb)),
            source_details=InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=boot_volume_size_gb,
            ),
            create_vnic_details=CreateVnicDetails(
                subnet_id=subnet_ocid,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": ssh_public_key},
        )

        try:
            response = compute_client.launch_instance(launch_details)
            instance = response.data
            logger.info("생성 성공! instance_ocid=%s", instance.id)

            # 공인 IP (VNIC가 붙을 때까지 잠깐 기다림)
            public_ip = _get_public_ip(compute_client, network_client, compartment_ocid, instance.id, max_wait=90)
            created_at = instance.time_created.isoformat() if instance.time_created else datetime.now(timezone.utc).isoformat()

            state.update(
                {
                    "success": True,
                    "instance_ocid": instance.id,
                    "public_ip": public_ip,
                    "created_at": created_at,
                    "last_error": None,
                }
            )
            save_state(state)
            notify_success(
                display_name=display_name,
                instance_ocid=instance.id,
                public_ip=public_ip,
                region=region,
                ad=ad_name,
                ocpus=ocpus,
                memory_gb=memory_gb,
                created_at=created_at,
                already_exists=False,
            )
            return 0

        except ServiceError as exc:
            # 8. 에러 처리
            msg = exc.message or ""
            code = exc.code or ""
            logger.warning("생성 실패: code=%s status=%s message=%s", code, exc.status, msg)

            # 자리 없음·429는 다음 cron에 다시
            if _is_out_of_capacity(code, msg, exc.status):
                logger.info("자리 없음(또는 429). 다음 시도를 기다려요.")
                state["last_error"] = f"{code}: {msg}"
                save_state(state)
                # 자리 없음 알림
                busy = code == "TooManyRequests" or exc.status == 429
                if busy:
                    state["backoff_until"] = time.time() + 600
                    save_state(state)
                discord_notify({
                    "title": "⏳ 요청 너무 많음 (429) — 10분 쉬고 다시" if busy else "🔁 자리 없음 — 다음에 다시",
                    "color": 0xFEE75C if busy else 0x5865F2,
                    "description": f"{state['attempt_count']}번째 시도 · {ad_name}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return 0

            # 그 밖의 ServiceError는 디스코드로 알리고 exit 1
            state["last_error"] = f"{code}: {msg}"
            save_state(state)
            notify_error(code, msg, exc.status)
            return 1
        except Exception as exc:  # noqa: BLE001
            logger.error("생성 중 예상 못 한 예외: %s", exc)
            state["last_error"] = str(exc)
            save_state(state)
            return 1

    # 모든 AD에서 실패
    logger.info("모든 AD에서 실패했어요. 다음 시도를 기다려요.")
    save_state(state)
    return 0


def _is_out_of_capacity(code: str, message: str, status: Any) -> bool:
    """자리 없음(Out of host capacity) 계열 에러인지."""
    msg_lower = (message or "").lower()
    if code == "TooManyRequests" or status == 429:
        return True
    if code == "InternalError" and "out of host capacity" in msg_lower:
        return True
    if status == 500 and "out of capacity" in msg_lower:
        return True
    if "out of host capacity" in msg_lower:
        return True
    return False


def _find_existing_instance(compute_client: Any, compartment_ocid: str, display_name: str) -> Any | None:
    """이름이 같고 PROVISIONING·RUNNING·STARTING 상태인 인스턴스."""
    active_states = {"PROVISIONING", "RUNNING", "STARTING"}
    instances = compute_client.list_instances(compartment_id=compartment_ocid).data
    for inst in instances:
        if inst.display_name == display_name and inst.lifecycle_state in active_states:
            return inst
    return None


def _find_latest_image(
    compute_client: Any,
    compartment_ocid: str,
    os_name: str,
    os_version: str,
    shape: str,
    arch: str,
) -> str:
    """list_images로 최신 aarch64 Ubuntu 이미지 OCID를 가져온다.

    list_images에는 architecture 옵션이 없어서 shape로 ARM 호환 이미지를
    고르고, OS·버전으로 거른 뒤 가장 최신(TIMECREATED DESC)을 쓴다.
    """
    images = compute_client.list_images(
        compartment_id=compartment_ocid,
        operating_system=os_name,
        operating_system_version=os_version,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data

    if not images:
        raise RuntimeError(
            f"조건(os={os_name}, version={os_version}, shape={shape}, arch={arch})에 맞는 이미지가 없어요."
        )

    # 혹시 몰라 aarch64·arm64 호환을 우선
    for img in images:
        # base_image_public_id·operating_system 등으로는 아키텍처를 알기 어려운 경우가 많다.
        # A1.Flex로 고른 결과는 전부 aarch64 호환이라 첫 번째를 쓴다.
        return img.id
    raise RuntimeError("이미지를 가져오지 못했어요.")


def _get_public_ip(
    compute_client: Any,
    network_client: Any,
    compartment_ocid: str,
    instance_id: str,
    max_wait: int = 90,
) -> str | None:
    """VNIC가 붙을 때까지 잠깐 기다리며 공인 IP를 가져온다."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            vnics = compute_client.list_vnic_attachments(
                compartment_id=compartment_ocid,
                instance_id=instance_id,
            ).data
            for vnic in vnics:
                if not vnic.vnic_id:
                    continue
                vnic_data = network_client.get_vnic(vnic.vnic_id).data
                if vnic_data.public_ip:
                    return vnic_data.public_ip
        except Exception as exc:  # noqa: BLE001
            logger.debug("공인 IP 가져오는 중 예외 (계속 시도): %s", exc)
        time.sleep(5)
    logger.warning("%s초 안에 공인 IP를 못 가져왔어요.", max_wait)
    return None


if __name__ == "__main__":
    sys.exit(main())