<script setup>
import { ref, nextTick, onMounted } from 'vue'
import api from '../api'

const emit = defineEmits(['authenticated'])
defineProps({ notice: { type: String, default: '' } })

const token = ref('')
const busy = ref(false)
const err = ref('')
const input = ref(null)

onMounted(() => nextTick(() => input.value?.focus()))

async function submit() {
  if (busy.value || !token.value.trim()) return
  busy.value = true
  err.value = ''
  try {
    await api.login(token.value.trim())
    token.value = ''
    emit('authenticated')
  } catch (e) {
    err.value = e.message || '登录失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-box" @submit.prevent="submit">
      <div class="brand">精灵帧工作室<small>SpriteFrameService</small></div>
      <p class="desc">该服务已启用访问认证，请输入访问令牌。</p>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <input
        ref="input"
        v-model="token"
        type="password"
        class="token-input"
        placeholder="访问令牌"
        autocomplete="current-password"
        :disabled="busy"
      />

      <p v-if="err" class="err-msg">{{ err }}</p>

      <button class="primary" type="submit" :disabled="busy || !token.trim()">
        {{ busy ? '验证中...' : '登录' }}
      </button>

      <p class="hint">
        令牌由服务端 <code>SPRITE_AUTH_TOKEN</code> 配置。
      </p>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  display: flex;
  height: 100%;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.login-box {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
  max-width: 340px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 24px;
}
.login-box .brand {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 2px;
}
.login-box .brand small {
  display: block;
  font-size: 11px;
  font-weight: 400;
  color: var(--text-dim);
  margin-top: 2px;
}
.desc {
  color: var(--text-dim);
  font-size: 12px;
  margin: 0;
}
.notice {
  margin: 0;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--warn, #d89a3a);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 4px;
}
.token-input {
  padding: 8px 10px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  font-family: inherit;
}
.err-msg {
  margin: 0;
  color: var(--err);
  font-size: 12px;
}
.hint {
  margin: 0;
  font-size: 11px;
  color: var(--text-dim);
}
</style>
