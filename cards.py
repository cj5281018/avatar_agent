from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from html import escape
from avatar_service.leave_confirmation import view

def install_cards(app: FastAPI, bridge, authenticate):
    from fastapi import Depends

    @app.get("/cards/{session}/{operation}", response_class=HTMLResponse)
    def show(session: str, operation: str, auth=Depends(authenticate)):
        with bridge.store.open() as db:
            card = view(bridge.confirmations.owned(db, auth[0]["id"], session, operation))
        fields = card["input"]
        labels = {"startDate": "开始日期", "endDate": "结束日期", "leaveType": "假别", "reason": "事由"}
        rows = "".join(f"<dt>{title}</dt><dd>{escape(str(fields.get(key, '')))}</dd>" for key,title in labels.items())
        return HTMLResponse(
            '<article lang="zh-CN"><h2>请假申请</h2><dl>' + rows + '</dl><p>' +
            escape(card["status"]) + '</p>' +
            ('<button type="button" data-action="confirm">确认提交</button>' if card["status"] in ("pending","confirmed") else '') +
            ('<p>申请单号 '+escape(card["leaveId"])+'</p>' if card["leaveId"] else '') + '</article>',
            headers={"Cache-Control":"no-store"})
