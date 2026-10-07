"""SmartEditor ONE bridge for saving and publishing generated blog drafts.

The editor object is private to Naver's page and may change without notice. Every
write is guarded by capability checks; this module fails closed when its model
or media upload service is unavailable.
"""
from __future__ import annotations

import asyncio
import base64
import random
from pathlib import Path

from core.config import load_config
from core.credentials import CredentialVaultUnavailable, load_naver_credentials
from core.session import LoginCompletedError, close_naver, open_naver


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _capture_native_quote(page, text: str) -> dict:
    """Read the inserted quotation from both the model and rendered editor DOM."""
    return await page.evaluate("""({text}) => {
      const root=document.querySelector('#mainFrame');
      const editor=root?.contentWindow?.SmartEditor?._editors?.blogpc001;
      const components=editor?.getDocumentData?.()?.document?.components||[];
      const quotes=components.filter(component=>component['@ctype']==='quotation');
      const normalize=value=>String(value||'').normalize('NFC').replace(/\\s+/g,'');
      const wanted=normalize(text);
      const getText=component=>{
        const values=[];
        const walk=value=>{
          if(Array.isArray(value)){value.forEach(walk);return;}
          if(!value||typeof value!=='object') return;
          if(value['@ctype']==='textNode'&&typeof value.value==='string') values.push(value.value);
          Object.values(value).forEach(walk);
        };
        walk(component);
        return values.join('');
      };
      const component=quotes.find(item=>normalize(getText(item)).includes(wanted)
        ||normalize(JSON.stringify(item)).includes(wanted))||null;
      const doc=root?.contentDocument;
      const sections=[...(doc?.querySelectorAll('.se-section-quotation')||[])];
      const section=sections.find(node=>normalize(node.innerText).includes(wanted))||sections.at(-1)||null;
      return {
        component,
        domText:section?.innerText||'',
        className:section?.className||'',
        quoteCount:quotes.length
      };
    }""", {"text": text})


async def _clear_editor_popup(page, frame) -> None:
    """Wait for editor overlays and dismiss a blocking popup when it is safe."""
    popup_selectors = (".se-popup-dim", ".se-help-popup", ".se-help-layer", ".se-popup", "[role='dialog']")
    for scope in (frame, page):
        overlays = [scope.locator(selector) for selector in popup_selectors]
        visible = []
        for overlay in overlays:
            for index in range(min(await overlay.count(), 3)):
                candidate = overlay.nth(index)
                try:
                    if await candidate.is_visible():
                        visible.append(candidate)
                except Exception:
                    continue
        if not visible:
            continue
        try:
            await page.keyboard.press("Escape")
            await visible[0].wait_for(state="hidden", timeout=1_500)
        except Exception:
            pass
        remaining = []
        for overlay in visible:
            try:
                if await overlay.is_visible():
                    remaining.append(overlay)
            except Exception:
                continue
        if not remaining:
            continue
        close_selectors = (
            ".se-help-popup [aria-label*='닫기']",
            ".se-help-popup [title*='닫기']",
            ".se-help-layer [aria-label*='닫기']",
            ".se-popup [aria-label*='닫기']",
            ".se-popup [title*='닫기']",
            ".se-popup [class*='close']",
            "[role='dialog'] [aria-label*='닫기']",
            "[role='dialog'] [title*='닫기']",
        )
        dismissed = False
        for selector in close_selectors:
            close_button = scope.locator(selector)
            for index in range(min(await close_button.count(), 3)):
                candidate = close_button.nth(index)
                try:
                    if await candidate.is_visible():
                        await candidate.click(timeout=2_000)
                        await remaining[0].wait_for(state="hidden", timeout=2_000)
                        dismissed = True
                        break
                except Exception:
                    continue
            if dismissed:
                break
        if not dismissed:
            raise RuntimeError("네이버 에디터 팝업이 발행 버튼을 가리고 있어요. 열린 크롬에서 팝업을 닫은 뒤 다시 시도해 주세요.")


async def _find_visible_in_frames(page, selectors: tuple[str, ...]):
    """Find a visible editor control despite Naver moving it between frames."""
    for editor_frame in page.frames:
        for selector in selectors:
            try:
                matches = editor_frame.locator(selector)
                for index in range(min(await matches.count(), 5)):
                    candidate = matches.nth(index)
                    if await candidate.is_visible():
                        return editor_frame, candidate
            except Exception:
                continue
    return None, None


async def _click_quotation_insert_option(page, style_value: str) -> bool:
    """Choose the exact requested insert style; never silently substitute another."""
    short_style = style_value.replace("quotation_", "", 1)
    for editor_frame in page.frames:
        try:
            options = editor_frame.locator(
                "button[data-group='documentToolbar'][data-name='quotation'][data-role='option'], "
                ".se-toolbar-option-insert-quotation button[data-role='option'], "
                ".se-toolbar-option-insert-quotation button, "
                "button[class*='se-toolbar-option-insert-quotation-'][data-value]"
            )
            visible = []
            for index in range(min(await options.count(), 12)):
                option = options.nth(index)
                if await option.is_visible():
                    value = (await option.get_attribute("data-value") or "").lower()
                    classes = (await option.get_attribute("class") or "").lower()
                    visible.append((option, value, classes))
            if not visible:
                continue
            selected = next((item for item in visible if item[1] in {style_value, short_style}
                             or style_value in item[2] or short_style in item[2]), None)
            if selected is None:
                continue
            await selected[0].click(timeout=5_000)
            return True
        except Exception:
            continue
    return False


async def _open_insert_options(page, feature: str) -> bool:
    """Open the small right-hand dropdown on SmartEditor's split insert button."""
    item_class = "insert-quotation" if feature == "quotation" else "insert-horizontal-line"
    selectors = (
        f"li.se-toolbar-item-{item_class} button.se-document-toolbar-select-option-button",
        f"li.se-toolbar-item-{item_class} button[class*='select-option-button']",
        f"button.se-document-toolbar-select-option-button[data-name='{'quotation' if feature == 'quotation' else 'horizontal-line'}']",
    )
    _, dropdown = await _find_visible_in_frames(page, selectors)
    if dropdown is None:
        return False
    await dropdown.click(timeout=5_000)
    return True


async def _click_horizontal_line_option(page, style_value: str) -> bool:
    """Choose a specific line from the horizontal-line dropdown."""
    for editor_frame in page.frames:
        try:
            options = editor_frame.locator(
                ".se-toolbar-option-insert-horizontal-line button[data-value], "
                "button[class*='insert-horizontal-line-line-button'][data-value], "
                "button[data-name='horizontal-line'][data-role='option'][data-value]"
            )
            for index in range(min(await options.count(), 12)):
                option = options.nth(index)
                if not await option.is_visible():
                    continue
                value = (await option.get_attribute("data-value") or "").lower()
                if value == style_value:
                    await option.click(timeout=5_000)
                    return True
        except Exception:
            continue
    return False


async def _restore_editor_title(frame, title: str) -> bool:
    """Write the title through SmartEditor's visible title field after body updates."""
    selectors = (
        "input[placeholder*='제목']",
        "textarea[placeholder*='제목']",
        ".se-documentTitle[contenteditable='true']",
        ".se-documentTitle [contenteditable='true']",
        "[data-a11y-title='제목'] [contenteditable='true']",
        "[contenteditable='true'][data-placeholder*='제목']",
        "[contenteditable='true'][aria-label*='제목']",
    )
    for selector in selectors:
        fields = frame.locator(selector)
        for index in range(min(await fields.count(), 2)):
            field = fields.nth(index)
            try:
                if not await field.is_visible():
                    continue
                await field.fill(title, timeout=2_000)
                try:
                    value = await field.input_value(timeout=1_000)
                except Exception:
                    value = await field.inner_text(timeout=1_000)
                if value.strip() == title:
                    return True
            except Exception:
                continue
    # The SmartEditor title is a document component in some builds, without an
    # input/placeholder attribute. Set and verify it through the editor API.
    try:
        return await frame.locator("body").evaluate("""async (body, title) => {
          const editor=body.ownerDocument.defaultView?.SmartEditor?._editors?.blogpc001;
          if(!editor?.setDocumentTitle||!editor?.getDocumentData) return false;
          await Promise.resolve(editor.setDocumentTitle(title));
          const doc=editor.getDocumentData();
          const component=doc?.document?.components?.find(item=>item['@ctype']==='documentTitle');
          return Boolean(component&&JSON.stringify(component).includes(title));
        }""", title)
    except Exception:
        return False


async def _show_publish_notice(page, message: str, is_error: bool = False,
                               blocking: bool = False, ready: bool = False) -> None:
    """Show persistent progress or error in a modal over the Naver editor."""
    try:
        await page.evaluate("""({message,isError,blocking,ready}) => {
          let modal=document.getElementById('autosns-publish-notice');
          if(!modal){
            modal=document.createElement('div');modal.id='autosns-publish-notice';
            Object.assign(modal.style,{position:'fixed',inset:'0',zIndex:'2147483647',display:'grid',placeItems:'center',padding:'24px',background:'#10182899',font:'15px/1.65 system-ui,sans-serif'});
            modal.innerHTML='<section role="alertdialog" aria-modal="true" style="box-sizing:border-box;width:min(560px,100%);padding:24px;border-radius:16px;background:white;box-shadow:0 20px 70px #0005"><span data-spinner aria-hidden="true" style="display:block;width:28px;height:28px;margin:0 auto 14px;border:3px solid #dce8f8;border-top-color:#245fe8;border-radius:50%;animation:autosns-spin .8s linear infinite"></span><strong data-title style="display:block;margin-bottom:10px;font-size:19px"></strong><p data-message style="margin:0 0 20px;white-space:pre-wrap;overflow-wrap:anywhere"></p><button data-close style="min-width:96px;height:40px;border:0;border-radius:8px;background:#245fe8;color:white;font:inherit;font-weight:700;cursor:pointer">확인</button></section>';
            const animation=document.createElement('style');animation.textContent='@keyframes autosns-spin{to{transform:rotate(360deg)}}';document.head.appendChild(animation);
            modal.querySelector('[data-close]').addEventListener('click',()=>modal.remove());
            document.body.appendChild(modal);
          }
          modal.dataset.blocking=String(Boolean(blocking)||modal.dataset.blocking==='true');
          modal.querySelector('[data-title]').textContent=isError?'작업이 중단됐습니다':(ready?'초안이 준비됐어요':(modal.dataset.blocking==='true'?'AI 초안을 작성하고 있어요':'네이버 에디터 작업 중'));
          modal.querySelector('[data-message]').textContent=message;
          const close=modal.querySelector('[data-close]');
          close.hidden=!(isError||ready);
          close.textContent=ready?'편집 시작':'확인';
          modal.querySelector('[data-spinner]').hidden=Boolean(isError||ready);
          modal.querySelector('section').style.border=isError?'2px solid #cf3d35':'2px solid #3478f6';
          Object.assign(modal.style,(isError||ready||modal.dataset.blocking==='true')
            ?{position:'fixed',inset:'0',display:'grid',placeItems:'center',padding:'24px',background:'#10182899',pointerEvents:'auto'}
            :{position:'fixed',inset:'12px 12px auto auto',display:'block',padding:'0',background:'transparent',pointerEvents:'none'});
          Object.assign(modal.querySelector('section').style,modal.dataset.blocking==='true'||isError||ready
            ?{width:'min(520px,calc(100vw - 24px))',padding:'20px 24px',fontSize:'15px'}
            :{width:'min(420px,calc(100vw - 24px))',padding:'12px 16px',fontSize:'13px'});
          if(ready){modal.dataset.blocking='false';modal.querySelector('[data-title]').textContent='초안이 준비됐어요';}
        }""", {"message": str(message)[:1200], "isError": is_error, "blocking": blocking, "ready": ready})
    except Exception:
        pass


async def _show_publish_error(page, error: Exception) -> None:
    """Keep the concrete failure in a dismissible modal on top of the editor."""
    await _show_publish_notice(page, f"{error}\n\n확인을 누를 때까지 이 창은 유지됩니다.", is_error=True)


async def _clear_publish_notice(page) -> None:
    try:
        await page.evaluate("document.getElementById('autosns-publish-notice')?.remove()")
    except Exception:
        pass


async def _open_publish_browser(playwright, general: dict, account_id: str,
                                credentials: dict | None):
    """Restore this account's Naver session first, then open its editor profile."""
    if credentials:
        try:
            await open_naver(playwright, general, stop_after_login=True,
                             credentials=credentials, account_id=account_id)
        except LoginCompletedError:
            pass
        except Exception as exc:
            raise RuntimeError(f"저장된 네이버 로그인 정보로 세션을 복구하지 못했어요: {exc}") from exc
        finally:
            credentials["username"] = ""
            credentials["password"] = ""
    return await open_naver(playwright, general, account_id=account_id)


async def _publish(post: dict, account_id: str, publish: bool = True,
                   session_prepared: bool = False) -> dict:
    from playwright.async_api import async_playwright

    if session_prepared:
        # The UI already ran the account setup login flow, which restored this
        # account's profile/cookies with the saved credentials. Don't launch a
        # second credential-entry browser before opening the editor.
        credentials = None
    else:
        try:
            credentials = load_naver_credentials(account_id)
        except CredentialVaultUnavailable:
            # A valid per-account browser profile/cookie session is enough to publish.
            # The keyring is only needed for unattended re-login after that session expires.
            credentials = None
    blog_id = credentials.get("username", "") if credentials else ""
    general = load_config().get("general", {})
    # Publishing is intentionally visible so the user can follow and intervene
    # in SmartEditor dialogs instead of having a hidden browser stall.
    publish_general = {**general, "show_browser": True}
    speed = general.get("task_speed", "medium")
    # These are the same three user-facing speed choices used by the rest of
    # the app. Keep a visible pause between blocks; don't dump the full body
    # into the editor in one update.
    delay_ranges = {"min": (1500, 2400), "medium": (900, 1600), "max": (450, 850)}
    delay_min_ms, delay_max_ms = delay_ranges.get(speed, delay_ranges["medium"])
    try:
        async with async_playwright() as playwright:
            _, page = await _open_publish_browser(playwright, publish_general, account_id, credentials)
            publish_succeeded = False
            try:
                await page.goto("https://blog.naver.com/MyBlog.naver", wait_until="domcontentloaded", timeout=35_000)
                await page.wait_for_timeout(500)
                if "blog.naver.com" in page.url and "MyBlog.naver" not in page.url:
                    resolved_blog_id = page.url.split("blog.naver.com/", 1)[1].split("?", 1)[0].split("/", 1)[0]
                    if resolved_blog_id and resolved_blog_id not in {"PostView.naver", "GoBlogWrite.naver"}:
                        blog_id = resolved_blog_id
                if not blog_id:
                    raise RuntimeError("연결된 블로그 주소를 찾지 못했어요. 블로그 계정을 다시 연결해 주세요.")
                await page.goto(f"https://blog.naver.com/{blog_id}?Redirect=Write", wait_until="domcontentloaded", timeout=45_000)
                if not publish:
                    await _show_publish_notice(page, "네이버 에디터를 열고 초안을 입력하고 있어요. 작업이 끝나면 ‘편집 시작’을 눌러 내용을 확인해 주세요.", blocking=True)
                await page.wait_for_timeout(1800)
                if "nid.naver.com" in page.url:
                    await close_naver()
                    try:
                        retry_credentials = load_naver_credentials(account_id)
                    except Exception:
                        retry_credentials = None
                    if not retry_credentials:
                        raise RuntimeError("네이버 로그인이 만료됐고 이 계정의 자동 로그인 정보가 없어요. 계정 연결에서 로그인 정보를 저장한 뒤 다시 시도해 주세요.")
                    _, page = await _open_publish_browser(playwright, publish_general, account_id, retry_credentials)
                    await page.goto("https://blog.naver.com/MyBlog.naver", wait_until="domcontentloaded", timeout=35_000)
                    await page.wait_for_timeout(500)
                    resolved_blog_id = page.url.split("blog.naver.com/", 1)[1].split("?", 1)[0].split("/", 1)[0] if "blog.naver.com/" in page.url else ""
                    if resolved_blog_id and resolved_blog_id not in {"MyBlog.naver", "PostView.naver", "GoBlogWrite.naver"}:
                        blog_id = resolved_blog_id
                    await page.goto(f"https://blog.naver.com/{blog_id}?Redirect=Write", wait_until="domcontentloaded", timeout=45_000)
                    if not publish:
                        await _show_publish_notice(page, "로그인을 확인했어요. 네이버 에디터에 초안을 입력하고 있어요.", blocking=True)
                    await page.wait_for_timeout(1800)
                    if "nid.naver.com" in page.url:
                        raise RuntimeError("저장된 네이버 로그인 정보로 다시 로그인했지만 에디터 접근이 거부됐어요. 본인 확인을 마친 뒤 다시 발행해 주세요.")
                from core.blog_writer import _ensure_quote_and_divider
                editor_blocks = [dict(block) for block in post.get("blocks", []) if isinstance(block, dict)]
                editor_blocks = _ensure_quote_and_divider(editor_blocks)
                for block in editor_blocks:
                    if block.get("type") == "divider":
                        block["style"] = "line1"
                referenced_images = {block.get("image_id") for block in editor_blocks if block.get("type") == "image"}
                payload_images = []
                for image in post.get("images", []):
                    file_path = Path(image.get("path", ""))
                    if not file_path.is_file():
                        raise RuntimeError(f"포스팅 이미지 파일을 찾지 못했어요: {image.get('id', '이미지')}")
                    payload_images.append({"id": image["id"], "name": f"autosns_{len(payload_images)+1:02d}.png", "data": base64.b64encode(file_path.read_bytes()).decode("ascii")})
                    if image.get("id") not in referenced_images:
                        image_block = {"type": "image", "editor_feature": "photo", "image_id": image["id"]}
                        first_paragraph = next((index for index, block in enumerate(editor_blocks) if block.get("type") == "paragraph"), None)
                        editor_blocks.insert((first_paragraph + 1) if first_paragraph is not None else len(editor_blocks), image_block)
                        referenced_images.add(image["id"])
                available_image_ids = {image["id"] for image in payload_images}
                missing_image_refs = [block.get("image_id") for block in editor_blocks if block.get("type") == "image" and block.get("image_id") not in available_image_ids]
                if missing_image_refs:
                    raise RuntimeError("본문에 연결된 이미지 파일이 누락되어 발행을 멈췄어요. 초안을 다시 생성해 주세요.")
                saved_quote_plan = post.get("quote_plan") if isinstance(post.get("quote_plan"), list) else []
                quote_plan = []
                for index, block in enumerate(editor_blocks):
                    if block.get("type") != "quote":
                        continue
                    saved = next((item for item in saved_quote_plan if isinstance(item, dict) and item.get("text") == block.get("text")), {})
                    quote_plan.append({
                        "block_index": index,
                        "text": block.get("text", ""),
                        "style": saved.get("style", block.get("style", "quotation_line")),
                    })
                quote_style_order = ["quotation_line", "quotation_bubble", "quotation_underline", "quotation_postit", "quotation_corner", "default"]
                quote_style_aliases = {
                    "vertical": "quotation_line", "line": "quotation_line", "bubble": "quotation_bubble",
                    "underline": "quotation_underline", "postit": "quotation_postit", "frame": "quotation_corner",
                    "quotation_frame": "quotation_corner", "quotation_default": "default",
                }
                quote_style_counts = {style: 0 for style in quote_style_order}
                previous_style = None
                for quote in quote_plan:
                    preferred = quote_style_aliases.get(str(quote["style"]).lower(), str(quote["style"]).lower())
                    if preferred not in quote_style_counts:
                        preferred = "quotation_line"
                    if quote_style_counts[preferred] == 0 and preferred != previous_style:
                        selected_style = preferred
                    else:
                        candidates = [style for style in quote_style_order if style != previous_style]
                        selected_style = min(candidates, key=lambda style: quote_style_counts[style])
                    quote["style"] = selected_style
                    quote_style_counts[selected_style] += 1
                    previous_style = selected_style
                payload = {
                    "title": post["title"], "blocks": editor_blocks, "images": payload_images,
                    "quote_plan": quote_plan,
                    "publish": publish,
                    "block_delay_ms": random.randint(delay_min_ms, delay_max_ms),
                }
                result = await page.evaluate("""async payload => {
                  const frame = document.querySelector('#mainFrame');
                  const win = frame?.contentWindow;
                  const editor = win?.SmartEditor?._editors?.blogpc001;
                  if (!editor?.getDocumentData || !editor?.setDocumentData || !editor?.setDocumentTitle) {
                    return {ok:false,error:'스마트에디터 구성 요소를 찾지 못했어요. 네이버 에디터 화면을 확인해 주세요.'};
                  }
                  const makeId = () => 'SE-' + crypto.randomUUID();
                  const paragraph = (text, variant='body') => ({'@ctype':'paragraph',id:makeId(),nodes:[{'@ctype':'textNode',id:makeId(),value:text,style:{'@ctype':'nodeStyle',fontSizeCode:variant==='heading'?'fs28':'fs19',bold:variant!=='body'}}],style:{'@ctype':'paragraphStyle',lineHeight:variant==='tableRow'?1.5:1.7}});
                  const uploaded = {};
                  if (payload.images.length) {
                    const service = editor._videoUploadService?._imageUploadService;
                    if (!service?.createSourceList || !service?.uploadImagesFromFiles) return {ok:false,error:'이미지 업로드 기능을 찾지 못했어요.'};
                    const files = payload.images.map(image => new File([Uint8Array.from(atob(image.data),c=>c.charCodeAt(0))],image.name,{type:'image/png'}));
                    const list = service.createSourceList(payload.images.map(image=>image.id),files);
                    const pending = await service.uploadImagesFromFiles(list);
                    const uploads = [];
                    for (const upload of pending) {
                      uploads.push(await upload);
                      await new Promise(resolve=>setTimeout(resolve,payload.block_delay_ms));
                    }
                    uploads.forEach((entry,index)=>{
                      if(entry.code!=='SUCCESS'||!entry.response) throw new Error('네이버 이미지 업로드에 실패했어요.');
                      const r=entry.response, file=payload.images[index];
                      const width=Math.min(693,r.width), height=Math.round(r.height*width/r.width);
                      uploaded[file.id]={'@ctype':'image',id:makeId(),layout:'default',src:r.domain+r.url+'?type=w1',path:r.url,domain:r.domain,internalResource:true,represent:index===0,fileSize:r.fileSize,fileName:r.fileName,originalWidth:r.width,originalHeight:r.height,width,height,widthPercentage:0,format:'normal',displayFormat:'normal',imageLoaded:true,contentMode:'fit',origin:{'@ctype':'imageOrigin',srcFrom:'local'},ai:false};
                    });
                  }
                const components=[];let paragraphs=[];let quoteCount=0;
                  const flush=()=>{if(paragraphs.length){components.push({'@ctype':'text',id:makeId(),layout:'default',value:paragraphs});paragraphs=[];}};
                  for(let blockIndex=0;blockIndex<payload.blocks.length;blockIndex++){
                    const block=payload.blocks[blockIndex];
                    const feature=block.editor_feature||({paragraph:'text',heading:'heading',quote:'quotation',table:'table',image:'photo',place:'place'}[block.type]);
                    if(feature==='text'||feature==='heading'){paragraphs.push(paragraph(block.text,feature==='heading'?'heading':'body'));flush();}
                    else if(feature==='quotation'){
                      flush();
                      const planned=payload.quote_plan.find(item=>item.block_index===blockIndex&&item.text===block.text);
                      if(!planned) return {ok:false,error:'AI 인용구 문장과 본문 블록이 일치하지 않아요. 초안을 다시 생성해 주세요.'};
                      components.push({__quotePlanIndex:blockIndex});
                      quoteCount++;
                    }
                    else if(feature==='divider'){
                      flush();
                      components.push({__dividerPlanIndex:blockIndex});
                    }
                    else if(feature==='photo'){flush();if(uploaded[block.image_id])components.push(uploaded[block.image_id]);}
                    else if(feature==='table'||feature==='place'){
                      return {ok:false,error:feature==='table'
                        ?'네이버 표는 표 도구로 삽입해야 해서 현재 자동 입력을 멈췄어요. 표 내용을 본문 문장으로 바꿔 다시 생성해 주세요.'
                        :'네이버 장소는 장소 검색 결과를 선택해야 첨부돼요. 장소 블록 자동 첨부가 준비되지 않아 발행을 멈췄어요.'};
                    }
                  }
                  flush();
                  const doc=editor.getDocumentData();
                  if(!doc?.document?.components) return {ok:false,error:'현재 글 문서 구조를 읽지 못했어요.'};
                  const titleComponent=doc.document.components.find(component=>component['@ctype']==='documentTitle');
                  doc.document.components=titleComponent?[titleComponent]:[];
                  await Promise.resolve(editor.setDocumentData(structuredClone(doc)));
                  return {ok:true,components,uploadedCount:Object.keys(uploaded).length,quoteCount};
                }""", payload)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error", "네이버 에디터에 내용을 넣지 못했어요."))
                expected_quote_count = sum(block.get("type") == "quote" for block in editor_blocks)
                expected_divider_count = sum(block.get("type") == "divider" for block in editor_blocks)
                if result.get("quoteCount", 0) != expected_quote_count:
                    raise RuntimeError("AI가 고른 인용구를 네이버 에디터에 모두 넣지 못했어요. 발행하지 않았습니다.")
                frame = page.frame_locator("#mainFrame")
                await _clear_editor_popup(page, frame)
                quote_components = {}
                quote_values = {
                    "default": "default", "quotation_default": "default",
                    "vertical": "quotation_line", "quotation_line": "quotation_line",
                    "bubble": "quotation_bubble", "quotation_bubble": "quotation_bubble",
                    "underline": "quotation_underline", "quotation_underline": "quotation_underline",
                    "postit": "quotation_postit", "quotation_postit": "quotation_postit",
                    "frame": "quotation_corner", "quotation_corner": "quotation_corner",
                    "quotation_frame": "quotation_corner",
                }
                for quote_number, quote in enumerate(quote_plan, start=1):
                    block_index = int(quote["block_index"])
                    quote_text = str(quote.get("text", "")).strip()
                    style_value = quote_values.get(str(quote.get("style", "")), "quotation_line")
                    if not quote_text:
                        raise RuntimeError("AI가 고른 인용구 문장이 비어 있어 발행을 멈췄어요.")
                    await _show_publish_notice(page, f"인용구 스타일 적용 중 · {quote_number}/{len(quote_plan)} · {style_value}")
                    toolbar_frame, toolbar = await _find_visible_in_frames(page, (
                        "button.se-toolbar-option-insert-quotation-select",
                        "button[data-group='documentToolbar'][data-name='quotation'][data-type='icon-select']",
                        "button[data-name='quotation'][data-type='icon-select']",
                        "button[title*='인용구']",
                        "button[aria-label*='인용구']",
                    ))
                    if toolbar is None:
                        frame_urls = ", ".join((item.url or "about:blank")[:100] for item in page.frames[:5])
                        raise RuntimeError(f"네이버 인용구 도구를 찾지 못했어요. 에디터 툴바가 로드됐는지 확인해 주세요. (프레임 {len(page.frames)}개: {frame_urls})")
                    if not await _open_insert_options(page, "quotation"):
                        raise RuntimeError("네이버 인용구 추가 버튼 오른쪽의 스타일 목록을 열지 못했어요. 발행을 멈췄습니다.")
                    if not await _click_quotation_insert_option(page, style_value):
                        raise RuntimeError(f"네이버 인용구 메뉴에서 요청한 스타일({style_value})을 찾지 못했어요. 기본 스타일로 바꾸지 않고 발행을 멈췄습니다.")
                    expected_layout = {"default": "se-l-default", "quotation_line": "se-l-quotation_line", "quotation_bubble": "se-l-quotation_bubble", "quotation_underline": "se-l-quotation_underline", "quotation_postit": "se-l-quotation_postit", "quotation_corner": "se-l-quotation_corner"}[style_value]
                    quote_target = frame.locator(".se-section-quotation .se-quote").last
                    quote_slot_ready = False
                    for _ in range(24):
                        try:
                            if await quote_target.count() and await quote_target.is_visible():
                                slot_class = await frame.locator(".se-section-quotation").last.get_attribute("class") or ""
                                if expected_layout in slot_class.split():
                                    quote_slot_ready = True
                                    break
                        except Exception:
                            pass
                        await page.wait_for_timeout(150)
                    if not quote_slot_ready:
                        raise RuntimeError(f"인용구 스타일({style_value})은 선택했지만 입력할 인용구 영역이 에디터에 만들어지지 않았어요. 발행을 멈췄습니다.")
                    # Selecting an insert style may close the dropdown without
                    # leaving keyboard focus in the new quotation block. Focus
                    # the quote text area explicitly before typing.
                    await quote_target.click(timeout=5_000)
                    await page.keyboard.insert_text(quote_text)
                    quote_state = {}
                    target_text = "".join(quote_text.split())
                    # SmartEditor updates its document model asynchronously;
                    # wait for both the visible text and the native model.
                    for _ in range(48):
                        quote_state = await _capture_native_quote(page, quote_text)
                        native_quote = quote_state.get("component")
                        visible_quote_text = "".join(str(quote_state.get("domText") or "").split())
                        if native_quote:
                            break
                        if target_text and target_text in visible_quote_text:
                            await page.wait_for_timeout(250)
                            continue
                        await page.wait_for_timeout(200)
                    if not native_quote:
                        visible_quote_text = str(quote_state.get("domText") or "").strip()
                        raise RuntimeError(f"인용구 영역은 만들었지만 문장이 에디터 문서에 저장되지 않았어요. 입력 영역 내용: {visible_quote_text[:120] or '비어 있음'} · 모델 인용구 수: {quote_state.get('quoteCount', 0)}. 발행을 멈췄습니다.")
                    # The dropdown choice inserts the quote in that layout.
                    # Do not click the context toolbar a second time: that can
                    # alter the active block/selection while the quote is being
                    # captured for document assembly.
                    layout_check = quote_state.get("className") or await page.evaluate("""({text}) => {
                      const doc=document.querySelector('#mainFrame')?.contentDocument;
                      const normalize=value=>String(value||'').normalize('NFC').replace(/\\s+/g,'');
                      const wanted=normalize(text);
                      const section=[...(doc?.querySelectorAll('.se-section-quotation')||[])].find(node=>normalize(node.innerText).includes(wanted));
                      return section?.className||null;
                    }""", {"text": quote_text})
                    if not layout_check or expected_layout not in layout_check.split():
                        raise RuntimeError(f"인용구 목록에서 고른 {style_value}가 실제 블록에 반영되지 않았어요 (현재: {layout_check or '확인 불가'}). 발행을 멈췄습니다.")
                    quote_components[block_index] = native_quote
                    await page.evaluate("""() => {
                      const editor=document.querySelector('#mainFrame')?.contentWindow?.SmartEditor?._editors?.blogpc001;
                      const doc=editor?.getDocumentData?.();
                      if(!doc?.document?.components) return;
                      const title=doc.document.components.find(component=>component['@ctype']==='documentTitle');
                      doc.document.components=title?[title]:[];
                      editor.setDocumentData(structuredClone(doc));
                    }""")
                    await page.wait_for_timeout(150)
                divider_components = {}
                divider_number = 0
                for block_index, block in enumerate(editor_blocks):
                    if block.get("type") != "divider":
                        continue
                    divider_number += 1
                    await _show_publish_notice(page, f"긴 구분선 적용 중 · {divider_number}/{expected_divider_count} · line1")
                    if not await _open_insert_options(page, "divider"):
                        raise RuntimeError("네이버 구분선 추가 버튼 오른쪽의 스타일 목록을 열지 못했어요. 발행을 멈췄습니다.")
                    if not await _click_horizontal_line_option(page, "line1"):
                        raise RuntimeError("네이버 구분선 스타일 목록에서 긴 가로선(line1)을 찾지 못했어요. 발행을 멈췄습니다.")
                    divider_capture = None
                    for _ in range(12):
                        divider_capture = await page.evaluate("""() => {
                          const editor=document.querySelector('#mainFrame')?.contentWindow?.SmartEditor?._editors?.blogpc001;
                          const components=editor?.getDocumentData?.()?.document?.components||[];
                          const component=[...components].reverse().find(item=>item['@ctype']==='horizontalLine')||null;
                          const doc=document.querySelector('#mainFrame')?.contentDocument;
                          const section=[...(doc?.querySelectorAll('.se-section-horizontalLine')||[])].at(-1);
                          return component?{component,className:section?.className||null}:null;
                        }""")
                        if divider_capture and divider_capture.get("component") and divider_capture.get("className"):
                            break
                        await page.wait_for_timeout(100)
                    native_divider = divider_capture.get("component") if divider_capture else None
                    divider_class = str((divider_capture or {}).get("className") or "")
                    if not native_divider or "se-l-line1" not in divider_class.split():
                        raise RuntimeError(f"구분선 메뉴를 눌렀지만 긴 가로선 적용을 확인하지 못했어요 (현재: {divider_class or '구분선 DOM 없음'}). 발행을 멈췄습니다.")
                    divider_components[block_index] = native_divider
                    await page.evaluate("""() => {
                      const editor=document.querySelector('#mainFrame')?.contentWindow?.SmartEditor?._editors?.blogpc001;
                      const doc=editor?.getDocumentData?.();
                      if(!doc?.document?.components) return;
                      const title=doc.document.components.find(component=>component['@ctype']==='documentTitle');
                      doc.document.components=title?[title]:[];
                      editor.setDocumentData(structuredClone(doc));
                    }""")
                    await page.wait_for_timeout(150)
                await _show_publish_notice(page, "선택한 인용구와 구분선을 본문 순서대로 조립하고 있어요.")
                components = [
                    quote_components[component["__quotePlanIndex"]]
                    if isinstance(component, dict) and "__quotePlanIndex" in component
                    else divider_components[component["__dividerPlanIndex"]]
                    if isinstance(component, dict) and "__dividerPlanIndex" in component
                    else component
                    for component in result.get("components", [])
                ]
                assembled = await page.evaluate("""async ({title,components,delay}) => {
                  const editor=document.querySelector('#mainFrame')?.contentWindow?.SmartEditor?._editors?.blogpc001;
                  if(!editor?.getDocumentData||!editor?.setDocumentData||!editor?.setDocumentTitle) return {ok:false};
                  const doc=editor.getDocumentData();
                  const titleComponent=doc?.document?.components?.find(component=>component['@ctype']==='documentTitle');
                  if(!doc?.document?.components) return {ok:false};
                  doc.document.components=titleComponent?[titleComponent]:[];
                  await Promise.resolve(editor.setDocumentData(structuredClone(doc)));
                  await Promise.resolve(editor.setDocumentTitle(title));
                  for(const component of components){
                    doc.document.components.push(component);
                    await Promise.resolve(editor.setDocumentData(structuredClone(doc)));
                    // Reapply after each document-model update; SmartEditor may
                    // otherwise overwrite a manually entered title with stale state.
                    await Promise.resolve(editor.setDocumentTitle(title));
                    await new Promise(resolve=>setTimeout(resolve,delay));
                  }
                  await Promise.resolve(editor.setDocumentTitle(title));
                  const frameDoc=document.querySelector('#mainFrame')?.contentDocument;
                  const renderedLayouts=[...(frameDoc?.querySelectorAll('.se-section-quotation')||[])].map(node=>node.className);
                  const renderedDividers=[...(frameDoc?.querySelectorAll('.se-section-horizontalLine')||[])].map(node=>{
                    const sectionWidth=node.getBoundingClientRect().width;
                    const lineWidth=node.querySelector('hr.se-hr')?.getBoundingClientRect().width||0;
                    return {className:node.className,widthRatio:sectionWidth?lineWidth/sectionWidth:0};
                  });
                  return {ok:true,componentCount:components.length,quoteCount:components.filter(component=>component['@ctype']==='quotation').length,renderedLayouts,renderedDividers};
                }""", {"title": post["title"], "components": components, "delay": payload["block_delay_ms"]})
                if not assembled.get("ok") or assembled.get("quoteCount") != expected_quote_count:
                    raise RuntimeError("AI 인용구를 포함한 본문을 네이버 에디터 문서로 구성하지 못했어요.")
                expected_layout_classes = [{"default": "se-l-default", "quotation_line": "se-l-quotation_line", "quotation_bubble": "se-l-quotation_bubble", "quotation_underline": "se-l-quotation_underline", "quotation_postit": "se-l-quotation_postit", "quotation_corner": "se-l-quotation_corner"}[quote["style"]] for quote in quote_plan]
                rendered_layouts = assembled.get("renderedLayouts", [])
                if not isinstance(rendered_layouts, list) or len(rendered_layouts) < expected_quote_count or any(expected not in str(rendered_layouts[index] or "").split() for index, expected in enumerate(expected_layout_classes)):
                    raise RuntimeError("인용구 스타일이 최종 본문에서 유지되지 않아 발행을 멈췄습니다. 네이버 에디터 결과를 확인해 주세요.")
                rendered_dividers = assembled.get("renderedDividers", [])
                if (not isinstance(rendered_dividers, list) or len(rendered_dividers) < expected_divider_count or any(
                    not isinstance(item, dict)
                    or "se-l-line1" not in str(item.get("className") or "").split()
                    or _safe_float(item.get("widthRatio")) < 0.7
                    for item in rendered_dividers[:expected_divider_count]
                )):
                    raise RuntimeError("긴 구분선이 최종 본문에 적용되지 않아 발행을 멈췄습니다. 네이버 에디터에서 구분선 스타일을 확인해 주세요.")
                # SmartEditor can reset its title model when body components are
                # replaced. Restore the visible field and fail closed if it did not stick.
                if not await _restore_editor_title(frame, post["title"]):
                    raise RuntimeError("본문 입력 뒤 제목을 에디터에서 확인하지 못해 발행을 멈췄어요. 제목 입력란 상태를 확인해 주세요.")
                if publish:
                    await _clear_publish_notice(page)
                else:
                    await _show_publish_notice(page, "제목, 본문, 이미지 입력을 마쳤습니다. 확인을 누르면 네이버 에디터에서 직접 수정할 수 있어요.", ready=True)
                if publish:
                    await _clear_editor_popup(page, frame)
                    publish_button = frame.get_by_role("button", name="발행", exact=True)
                    page_publish_button = page.get_by_role("button", name="발행", exact=True)
                    if await publish_button.count() == 0 and await page_publish_button.count() == 0:
                        raise RuntimeError("본문은 에디터에 입력했지만 발행 버튼을 찾지 못했어요. 임시 저장 상태로 남아 있을 수 있습니다.")
                    trigger = publish_button if await publish_button.count() else page_publish_button
                    await asyncio.sleep(random.uniform(delay_min_ms, delay_max_ms) / 1000)
                    await trigger.first.click(timeout=8_000)
                    await page.wait_for_timeout(random.randint(delay_min_ms, delay_max_ms))
                    confirmation_clicked = False
                    for scope in (frame, page):
                        dialogs = scope.get_by_role("dialog")
                        if await dialogs.count():
                            confirm = dialogs.last.get_by_role("button", name="발행", exact=True)
                            if await confirm.count():
                                await asyncio.sleep(random.uniform(delay_min_ms, delay_max_ms) / 1000)
                                await confirm.last.click(timeout=8_000)
                                confirmation_clicked = True
                                break
                    if not confirmation_clicked:
                        for scope in (frame, page):
                            confirms = scope.get_by_role("button", name="발행", exact=True)
                            if await confirms.count() > 1:
                                await asyncio.sleep(random.uniform(delay_min_ms, delay_max_ms) / 1000)
                                await confirms.last.click(timeout=8_000)
                                confirmation_clicked = True
                                break
                    # Naver may keep the editor URL after a successful publish,
                    # and its completion message can live inside mainFrame.
                    # Poll every frame before deciding that the publish failed.
                    await page.wait_for_timeout(random.randint(delay_min_ms * 2, delay_max_ms * 2))
                    completion_text = ""
                    for _ in range(12):
                        frame_texts = []
                        for current_frame in page.frames:
                            try:
                                frame_texts.append(await current_frame.locator("body").inner_text(timeout=800))
                            except Exception:
                                continue
                        completion_text = " ".join(frame_texts).lower()
                        if any(text in completion_text for text in ("발행이 완료", "발행되었습니다", "등록되었습니다", "글이 등록")):
                            break
                        if "Redirect=Write" not in page.url:
                            break
                        await page.wait_for_timeout(500)
                    publish_error_markers = ("발행에 실패", "발행하지 못", "등록할 수 없습니다", "오류가 발생", "제한되어 발행", "제목을 입력해 주세요", "본문을 입력해 주세요")
                    if any(text in completion_text for text in publish_error_markers):
                        raise RuntimeError("네이버가 발행을 거절했습니다. 발행 화면의 안내를 확인해 주세요.")
                    # The final confirmation click is the strongest signal when
                    # Naver keeps Redirect=Write and renders no success toast.
                    # Do not report failure after that accepted click; it can
                    # cause users to retry and publish a duplicate post.
                    if "Redirect=Write" in page.url and not confirmation_clicked and not any(
                        text in completion_text for text in ("발행이 완료", "발행되었습니다", "등록되었습니다", "글이 등록")
                    ):
                        raise RuntimeError("네이버 발행 완료 화면을 확인하지 못했습니다. 발행 상태를 블로그에서 확인한 뒤 재시도해 주세요.")
                publish_succeeded = True
                return {"ok": True, "url": page.url, "components": assembled.get("componentCount", 0), "images": result.get("uploadedCount", 0), "quotes": assembled.get("quoteCount", 0), "draft_ready": not publish}
            except Exception as exc:
                await _show_publish_error(page, exc)
                # Keep the browser and modal open until the user acknowledges it.
                # This avoids the error disappearing when Playwright owns Chrome.
                try:
                    await page.wait_for_function(
                        "!document.getElementById('autosns-publish-notice')",
                        timeout=0,
                    )
                except Exception:
                    pass
                raise
            finally:
                if publish_succeeded and publish:
                    await close_naver()
    finally:
        if credentials:
            credentials["username"] = ""
            credentials["password"] = ""


def publish_blog_post(post: dict, account_id: str, session_prepared: bool = False,
                      publish: bool = True) -> dict:
    """Run SmartEditor ONE in the existing per-account browser session."""
    if not post.get("title") or not post.get("blocks"):
        raise ValueError("발행할 제목과 본문 블록이 없어요.")
    return asyncio.run(_publish(post, account_id, publish=publish, session_prepared=session_prepared))
