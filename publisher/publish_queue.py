"""GitHub 서버에서 실행돼요. (사용자 컴퓨터가 꺼져 있어도 동작)

GitHub 예약 실행은 몇십 분씩 늦을 때가 많아요. 그래서 게시 시각 30~40분 전에 미리 깨어나
게시 시각까지 기다렸다가 정확히 올려요.

queue/<날짜>-<morning|noon>/post.json 중
  - status가 "scheduled" 이고
  - publish_at(게시 시각)이 지났거나 EARLY_MIN분 안으로 다가왔으면
(다가온 경우엔 그 시각까지 기다렸다가) 인스타그램에 캐러셀로 게시하고,
결과를 post.json에 기록한 뒤 텔레그램으로 알려요.

비밀 값(토큰)은 GitHub 저장소의 Secrets에서만 읽어요. 파일에는 저장하지 않아요.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QUEUE = Path("queue")
GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v27.0')}"
MAX_TRIES = 3
EARLY_MIN = 45  # 게시 시각 몇 분 전부터 미리 깨어나 기다릴지


def http(method, url, params=None):
    body = None
    if params and method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    elif params:
        body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "ChicagoKnewsPublisher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message")
        except Exception:
            msg = ""
        raise RuntimeError(f"{e.code} {msg}") from None


def ig(method, path, params=None):
    p = dict(params or {})
    p["access_token"] = os.environ["INSTAGRAM_ACCESS_TOKEN"]
    return http(method, f"{GRAPH}/{path}", p)


def notify(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        http("POST", f"https://api.telegram.org/bot{tok}/sendMessage", {"chat_id": chat, "text": text})
    except Exception:
        pass


def wait_ready(cid, label):
    for _ in range(40):
        st = ig("GET", cid, {"fields": "status_code"}).get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{label} 처리 실패 ({st})")
        time.sleep(5)
    raise RuntimeError(f"{label} 처리 시간 초과")


def publish(post):
    uid = os.environ["INSTAGRAM_USER_ID"]
    alts = post.get("alt_texts", [])
    children = []
    for i, url in enumerate(post["images"]):
        params = {"image_url": url, "is_carousel_item": "true"}
        if i < len(alts) and alts[i]:
            params["alt_text"] = alts[i][:1000]
        try:
            cid = ig("POST", f"{uid}/media", params)["id"]
        except RuntimeError:
            params.pop("alt_text", None)
            cid = ig("POST", f"{uid}/media", params)["id"]
        wait_ready(cid, f"{i + 1}번 사진")
        children.append(cid)
    carousel = ig("POST", f"{uid}/media", {"media_type": "CAROUSEL", "children": ",".join(children),
                                            "caption": post["caption"][:2200]})["id"]
    wait_ready(carousel, "게시물")
    media_id = ig("POST", f"{uid}/media_publish", {"creation_id": carousel})["id"]
    return media_id, ig("GET", media_id, {"fields": "permalink"}).get("permalink", "")


def connection_check():
    """queue/_check.json 이 있으면 Secrets로 인스타·텔레그램 연결만 시험 (게시는 안 함)"""
    flag = QUEUE / "_check.json"
    if not flag.exists():
        return
    try:
        me = ig("GET", os.environ["INSTAGRAM_USER_ID"], {"fields": "username"})
        notify(f"✅ GitHub 연결 확인! @{me.get('username')} 계정으로 예약 게시할 준비가 됐어요. (실제 게시는 안 했어요)")
        print("연결 확인 성공")
    except Exception as e:
        notify(f"⚠️ GitHub 연결 확인 실패: {e}\nSecrets 값을 다시 확인해 주세요.")
        print(f"연결 확인 실패: {e}")
    flag.unlink()


def still_scheduled(pj):
    """기다리는 사이 다른 실행이 먼저 올렸을 수 있어서, 올리기 직전에 최신 기록을 다시 확인해요"""
    try:
        subprocess.run(["git", "pull", "--ff-only", "-q"], check=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"최신 기록 확인 실패 ({e}), 안전하게 이번엔 건너뛰어요")
        return None
    post = json.loads(pj.read_text(encoding="utf-8"))
    return post if post.get("status") == "scheduled" else None


def main():
    connection_check()
    due = []
    for pj in QUEUE.glob("*/post.json"):
        post = json.loads(pj.read_text(encoding="utf-8"))
        if post.get("status") != "scheduled":
            continue
        when = datetime.fromisoformat(post["publish_at"])
        if (when - datetime.now(timezone.utc)).total_seconds() <= EARLY_MIN * 60:
            due.append((when, pj, post))
    for when, pj, post in sorted(due, key=lambda x: x[0]):
        wait = (when - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            print(f"{pj.parent.name}: 게시 시각까지 {wait / 60:.1f}분 기다려요")
            time.sleep(wait)
        post = still_scheduled(pj)
        if post is None:
            print(f"{pj.parent.name}: 이미 올라갔거나 확인이 안 돼서 건너뛰어요 (중복 방지)")
            continue
        now = datetime.now(timezone.utc)
        label = post.get("label", pj.parent.name)
        try:
            media_id, link = publish(post)
            post.update(status="posted", media_id=media_id, permalink=link,
                        posted_at=now.isoformat(timespec="seconds"))
            notify(f"🎉 {label} 게시 완료!\n{link}")
            print(f"게시 완료: {pj.parent.name} {link}")
        except Exception as e:
            post["tries"] = post.get("tries", 0) + 1
            post["last_error"] = str(e)[:300]
            if post["tries"] >= MAX_TRIES:
                post["status"] = "failed"
                notify(f"⚠️ {label} 게시 실패 ({e}).\n텔레그램의 카드 이미지로 직접 올려 주세요.")
            print(f"실패: {pj.parent.name} {e}")
        pj.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
