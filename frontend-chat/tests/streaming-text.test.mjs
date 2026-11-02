import assert from 'node:assert/strict'
import { test } from 'node:test'
import { effectScope, nextTick, ref, watch } from 'vue'
import { useStreamingText } from '../src/composables/useStreamingText.ts'

function setup(t, initial = '', active = true) {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let now = 0
  t.mock.method(performance, 'now', () => now)
  const source = ref(initial)
  const streaming = ref(active)
  const scope = effectScope()
  const text = scope.run(() => useStreamingText(() => source.value, () => streaming.value))
  t.after(() => scope.stop())
  return { source, streaming, text, scope, advance(ms) { now += ms; t.mock.timers.tick(ms) } }
}

test('bursts render the first text immediately and the latest snapshot at 100ms', async t => {
  const s = setup(t)
  s.source.value = 'first'
  await nextTick()
  assert.equal(s.text.value, 'first')
  for (let n = 1; n <= 9; n++) {
    s.advance(10)
    s.source.value = `snapshot ${n}`
    await nextTick()
    assert.equal(s.text.value, 'first')
  }
  s.advance(10)
  assert.equal(s.text.value, 'snapshot 9')
  s.advance(200)
  s.source.value = 'after a quiet period'
  await nextTick()
  assert.equal(s.text.value, 'after a quiet period')
})

for (const status of ['completed', 'failed', 'cancelled']) {
  test(`${status} flushes the full final snapshot without waiting for the timer`, async t => {
    const s = setup(t, 'first')
    s.source.value = 'pending'
    await nextTick()
    assert.equal(s.text.value, 'first')
    s.source.value = `final ${status}`
    s.streaming.value = false
    await nextTick()
    assert.equal(s.text.value, `final ${status}`)
    s.advance(100)
    assert.equal(s.text.value, `final ${status}`)
  })
}

test('snapshot replacement and reconnect rollback replace text without concatenation', async t => {
  const s = setup(t, 'old longer snapshot')
  s.source.value = 'short'
  await nextTick()
  s.advance(100)
  assert.equal(s.text.value, 'short')
  s.source.value = ''
  await nextTick()
  s.streaming.value = false
  await nextTick()
  assert.equal(s.text.value, '')
})

test('completed messages remain cached across unrelated updates', async t => {
  const s = setup(t, 'history', false)
  const unrelated = ref(0)
  let renders = 0
  const stop = watch(s.text, () => renders++)
  unrelated.value++
  await nextTick()
  s.advance(1000)
  assert.equal(s.text.value, 'history')
  assert.equal(renders, 0)
  stop()
})

test('disposing a message cancels its pending timer', async t => {
  const s = setup(t, 'first')
  s.source.value = 'pending'
  await nextTick()
  s.scope.stop()
  s.advance(1000)
  assert.equal(s.text.value, 'first')
})

test('parallel conversations keep independent throttles', async t => {
  const s = setup(t, 'first')
  const otherSource = ref('other')
  const other = s.scope.run(() => useStreamingText(() => otherSource.value, () => true))
  s.source.value = 'first updated'
  otherSource.value = 'other updated'
  await nextTick()
  s.advance(100)
  assert.equal(s.text.value, 'first updated')
  assert.equal(other.value, 'other updated')
})
