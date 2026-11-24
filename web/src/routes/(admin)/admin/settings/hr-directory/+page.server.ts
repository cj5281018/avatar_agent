import { env } from '$env/dynamic/private'
import { error, fail } from '@sveltejs/kit'
import { requireAdmin } from '$lib/server/authHelpers'
import { userRepository } from '$lib/server/db/users'
import type { Actions, PageServerLoad } from './$types'

export const load: PageServerLoad = ({ locals }) => {
    requireAdmin(locals)
}

export const actions: Actions = {
    default: async ({ locals, request, fetch }) => {
        if (!locals.session || locals.user?.role !== 'admin') error(403, 'HR directory administration is restricted to administrators')
        const fields = await request.formData()
        const email = String(fields.get('email') ?? '').trim()
        const handle = String(fields.get('handle') ?? '').trim().toLowerCase()
        if (!email || email.length > 254 || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(handle)) {
            return fail(400, { message: '请输入工作台邮箱和有效的 HR 用户名' })
        }

        const account = await userRepository.findByEmail(email)
        if (!account?.isActive) return fail(404, { message: '找不到有效的工作台账号' })
        if (!env.AVATAR2_GATEWAY_URL || !env.AVATAR2_INTERNAL_TOKEN) error(503, 'HR 网关未配置')

        const response = await fetch(env.AVATAR2_GATEWAY_URL + '/internal/hr-directory', {
            method: 'POST',
            headers: {
                authorization: 'Bearer ' + env.AVATAR2_INTERNAL_TOKEN,
                'content-type': 'application/json',
            },
            body: JSON.stringify({ user_id: account.id, handle }),
            signal: AbortSignal.timeout(10_000),
        }).catch(() => null)

        if (response?.status === 409) return fail(409, { message: '该账号或 HR 身份已有其他关联' })
        if (response?.status === 404) return fail(404, { message: '找不到该 HR 身份' })
        if (!response?.ok) return fail(502, { message: 'HR 网关暂时不可用，请稍后重试' })
        return { message: account.email + ' 已关联 HR 身份 ' + handle }
    },
}
