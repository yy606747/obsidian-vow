package app.obsidianvow.core

import android.content.Context
import android.util.Log
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.feature.ExperimentalFeatureAvailabilityApi
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.OxygenSaturationRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import kotlinx.coroutines.runBlocking
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.temporal.ChronoUnit
import kotlin.reflect.KClass


data class HealthAuthorizationStatus(
    val sdkAvailable: Boolean,
    val grantedDataPermissions: Set<String>,
    val backgroundSupported: Boolean,
    val backgroundGranted: Boolean,
)


/**
 * Two deliberately separate paths: a fresh scalar tick for the live prompt,
 * and record-level snapshot/Changes batches for durable daily aggregation.
 * Every record type has its own permission check and Changes token.
 */
@OptIn(ExperimentalFeatureAvailabilityApi::class)
class HealthConnectReporter(
    private val ctx: Context,
    private val http: OkHttpClient,
    private val httpBase: String?,
) {
    private val tag = "ObsidianHealth"
    private val jsonMedia = "application/json; charset=utf-8".toMediaType()
    private val prefs = ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun isReady(): Boolean {
        return try {
            val status = HealthConnectClient.getSdkStatus(ctx)
            if (status != HealthConnectClient.SDK_AVAILABLE) {
                Log.i(tag, "HealthConnect unavailable, sdkStatus=$status")
                false
            } else {
                true
            }
        } catch (e: Exception) {
            Log.w(tag, "isReady failed: ${e.message}")
            false
        }
    }

    /** Synchronous JNI-friendly entry point; caller already runs on ObsidianSensing. */
    fun reportOnce() {
        val base = httpBase?.trimEnd('/') ?: return
        if (!isReady()) return

        runBlocking {
            try {
                ensureServerIdentity(base)
                val client = HealthConnectClient.getOrCreate(ctx)
                val granted = client.permissionController.getGrantedPermissions()
                val allowedNames = HealthPermissionPolicy.allowedKinds(
                    PERMISSIONS_BY_KIND,
                    granted,
                )
                val allowed = HcKind.entries.filter { it.wireName in allowedNames }
                val backgroundSupported = backgroundReadSupported(client)
                val backgroundGranted =
                    HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND in granted
                Log.i(
                    tag,
                    "authorizedTypes=${allowed.map { it.wireName }} " +
                        "backgroundSupported=$backgroundSupported " +
                        "backgroundGranted=$backgroundGranted",
                )

                val serverZone = bootstrapServerTimezone(base) ?: run {
                    Log.w(tag, "historical sync skipped: daily_timezone bootstrap failed")
                    return@runBlocking
                }

                reportFreshTick(client, allowed.toSet(), serverZone, base)
                for (kind in allowed) {
                    try {
                        syncKind(client, kind, serverZone, base)
                    } catch (e: Exception) {
                        Log.w(tag, "${kind.wireName} sync failed; token retained: ${e.message}")
                    }
                }
            } catch (e: Exception) {
                Log.w(tag, "reportOnce failed: ${e.message}")
            }
        }
    }

    private suspend fun syncKind(
        client: HealthConnectClient,
        kind: HcKind,
        serverZone: ZoneId,
        base: String,
    ) {
        val storedToken = prefs.getString(kind.tokenKey, null)
        if (storedToken.isNullOrBlank()) {
            initialSync(client, kind, serverZone, base)
            return
        }
        if (consumeChanges(client, kind, storedToken, serverZone, base) == SyncOutcome.EXPIRED) {
            Log.i(tag, "${kind.wireName} Changes token expired; rebuilding 14-day snapshot")
            prefs.edit().remove(kind.tokenKey).apply()
            initialSync(client, kind, serverZone, base)
        }
    }

    private suspend fun initialSync(
        client: HealthConnectClient,
        kind: HcKind,
        serverZone: ZoneId,
        base: String,
    ) {
        // Anchor before snapshot: changes racing the snapshot are consumed after it.
        val anchor = client.getChangesToken(ChangesTokenRequest(setOf(kind.recordType)))
        val now = Instant.now()
        if (!postSnapshot(client, kind, serverZone, now, base)) {
            Log.w(tag, "${kind.wireName} snapshot POST failed; initial token not saved")
            return
        }
        when (consumeChanges(client, kind, anchor, serverZone, base)) {
            SyncOutcome.SUCCESS -> Log.i(tag, "${kind.wireName} initial snapshot caught up")
            SyncOutcome.EXPIRED -> Log.w(tag, "${kind.wireName} fresh Changes token expired")
            SyncOutcome.FAILED -> Unit
        }
    }

    private suspend fun consumeChanges(
        client: HealthConnectClient,
        kind: HcKind,
        initialToken: String,
        serverZone: ZoneId,
        base: String,
    ): SyncOutcome {
        var token = initialToken
        while (true) {
            val response = client.getChanges(token)
            if (response.changesTokenExpired) return SyncOutcome.EXPIRED
            val records = response.changes
                .filterIsInstance<UpsertionChange>()
                .map { it.record }
            val deletions = response.changes.filterIsInstance<DeletionChange>()
            for (deletion in deletions) {
                Log.i(tag, "${kind.wireName} deletion deferred id=${deletion.recordId}")
            }
            Log.i(
                tag,
                "${kind.wireName} changes upserts=${records.size} " +
                    "deletions=${deletions.size} hasMore=${response.hasMore}",
            )
            if (!postChangedRecords(client, kind, records, serverZone, base)) {
                Log.w(tag, "${kind.wireName} changes POST failed; token retained")
                return SyncOutcome.FAILED
            }

            // Only advance after every batch derived from this page got 2xx.
            token = response.nextChangesToken
            prefs.edit().putString(kind.tokenKey, token).apply()
            if (!response.hasMore) return SyncOutcome.SUCCESS
        }
    }

    private suspend fun postSnapshot(
        client: HealthConnectClient,
        kind: HcKind,
        serverZone: ZoneId,
        now: Instant,
        base: String,
    ): Boolean {
        val today = now.atZone(serverZone).toLocalDate()
        val historyStart = today.minusDays(HISTORY_DAYS - 1L)
            .atStartOfDay(serverZone)
            .toInstant()
        val window = TimeRangeFilter.between(historyStart, now)

        return when (kind) {
            HcKind.HEART_RATE -> {
                val records = readAllRecords(client, HeartRateRecord::class, window)
                val encoded = records.mapNotNull(::heartRateJson)
                Log.i(
                    tag,
                    "heart_rate snapshot records=${records.size} " +
                        "samples=${records.sumOf { it.samples.size }}",
                )
                postRecordBatches(kind.arrayName, encoded, base)
            }
            HcKind.SPO2 -> {
                val records = readAllRecords(client, OxygenSaturationRecord::class, window)
                Log.i(tag, "spo2 snapshot records=${records.size}")
                postRecordBatches(kind.arrayName, records.map(::spo2Json), base)
            }
            HcKind.SLEEP -> {
                val records = readAllRecords(client, SleepSessionRecord::class, window)
                Log.i(
                    tag,
                    "sleep snapshot sessions=${records.size} " +
                        "stages=${records.sumOf { it.stages.size }}",
                )
                postRecordBatches(kind.arrayName, records.map(::sleepJson), base)
            }
            HcKind.STEPS -> {
                val dates = (0 until HISTORY_DAYS).map {
                    today.minusDays((HISTORY_DAYS - 1L) - it)
                }.toSet()
                val encoded = aggregateStepDates(client, dates, serverZone, now)
                Log.i(tag, "steps snapshot days=${dates.size} populated=${encoded.size}")
                postRecordBatches(kind.arrayName, encoded, base)
            }
        }
    }

    private suspend fun postChangedRecords(
        client: HealthConnectClient,
        kind: HcKind,
        records: List<Record>,
        serverZone: ZoneId,
        base: String,
    ): Boolean {
        val encoded = when (kind) {
            HcKind.HEART_RATE -> records.filterIsInstance<HeartRateRecord>()
                .mapNotNull(::heartRateJson)
            HcKind.SPO2 -> records.filterIsInstance<OxygenSaturationRecord>()
                .map(::spo2Json)
            HcKind.SLEEP -> records.filterIsInstance<SleepSessionRecord>()
                .map(::sleepJson)
            HcKind.STEPS -> {
                val now = Instant.now()
                val dates = affectedStepDates(
                    records.filterIsInstance<StepsRecord>(),
                    serverZone,
                    now,
                )
                aggregateStepDates(client, dates, serverZone, now)
            }
        }
        return postRecordBatches(kind.arrayName, encoded, base)
    }

    private suspend fun reportFreshTick(
        client: HealthConnectClient,
        allowed: Set<HcKind>,
        serverZone: ZoneId,
        base: String,
    ) {
        val now = Instant.now()
        val cutoff = now.minus(FRESH_MINUTES, ChronoUnit.MINUTES)
        val window = TimeRangeFilter.between(cutoff, now)
        val body = JSONObject().put("timestamp", epochSeconds(now))

        if (HcKind.HEART_RATE in allowed) {
            try {
                val records = readAllRecords(client, HeartRateRecord::class, window)
                val samples = records.flatMap { it.samples }
                    .filter { !it.time.isBefore(cutoff) && !it.time.isAfter(now) }
                val latest = samples.maxByOrNull { it.time }
                Log.i(tag, "heart_rate fresh records=${records.size} samples=${samples.size}")
                if (latest != null) {
                    body.put("heart_rate", latest.beatsPerMinute)
                    body.put("heart_rate_observed_at", epochSeconds(latest.time))
                }
            } catch (e: Exception) {
                Log.w(tag, "heart_rate fresh read failed: ${e.message}")
            }
        }

        if (HcKind.SPO2 in allowed) {
            try {
                val records = readAllRecords(client, OxygenSaturationRecord::class, window)
                val latest = records
                    .filter { !it.time.isBefore(cutoff) && !it.time.isAfter(now) }
                    .maxByOrNull { it.time }
                Log.i(tag, "spo2 fresh records=${records.size}")
                if (latest != null) {
                    body.put("spo2", latest.percentage.value.toInt())
                    body.put("spo2_observed_at", epochSeconds(latest.time))
                }
            } catch (e: Exception) {
                Log.w(tag, "spo2 fresh read failed: ${e.message}")
            }
        }

        if (HcKind.STEPS in allowed) {
            try {
                val records = readAllRecords(client, StepsRecord::class, window)
                val delta = records.sumOf { it.count }
                Log.i(tag, "steps fresh records=${records.size} delta=$delta")
                if (delta > 0) body.put("steps_delta", delta.toInt())
            } catch (e: Exception) {
                Log.w(tag, "steps fresh read failed: ${e.message}")
            }

            try {
                val localDate = now.atZone(serverZone).toLocalDate()
                val dayStart = localDate.atStartOfDay(serverZone).toInstant()
                val aggregate = client.aggregate(
                    AggregateRequest(
                        metrics = setOf(StepsRecord.COUNT_TOTAL),
                        timeRangeFilter = TimeRangeFilter.between(dayStart, now),
                    )
                )
                val totalToday = aggregate[StepsRecord.COUNT_TOTAL]
                if (totalToday != null) {
                    body.put("steps_total_today", totalToday)
                    body.put("steps_total_date", localDate.toString())
                    body.put("steps_total_timezone", serverZone.id)
                }
            } catch (e: Exception) {
                Log.w(tag, "steps daily aggregate failed: ${e.message}")
            }
        }

        if (HcKind.SLEEP in allowed) {
            try {
                val sleepWindow = TimeRangeFilter.between(
                    now.minus(10, ChronoUnit.HOURS),
                    now,
                )
                val records = readAllRecords(client, SleepSessionRecord::class, sleepWindow)
                val active = records.lastOrNull {
                    it.startTime.isBefore(now) &&
                        it.endTime.isAfter(now.minus(10, ChronoUnit.MINUTES))
                }
                Log.i(tag, "sleep fresh sessions=${records.size}")
                if (active != null) {
                    val stage = active.stages.lastOrNull {
                        it.startTime.isBefore(now) &&
                            it.endTime.isAfter(now.minus(10, ChronoUnit.MINUTES))
                    }
                    body.put("sleep_stage", stageName(stage?.stage))
                }
            } catch (e: Exception) {
                Log.w(tag, "sleep fresh read failed: ${e.message}")
            }
        }

        if (body.length() > 1) {
            postJson(base, "/api/biometrics/tick", body)
        } else {
            Log.i(tag, "no fresh HC values for tick")
        }
    }

    private suspend fun <T : Record> readAllRecords(
        client: HealthConnectClient,
        recordType: KClass<T>,
        window: TimeRangeFilter,
    ): List<T> {
        val records = mutableListOf<T>()
        var pageToken: String? = null
        var pages = 0
        do {
            val response = client.readRecords(
                ReadRecordsRequest(
                    recordType = recordType,
                    timeRangeFilter = window,
                    ascendingOrder = true,
                    pageSize = READ_PAGE_SIZE,
                    pageToken = pageToken,
                )
            )
            records.addAll(response.records)
            pages += 1
            pageToken = response.pageToken?.takeIf { it.isNotBlank() }
        } while (pageToken != null)
        Log.d(tag, "read ${recordType.simpleName} pages=$pages records=${records.size}")
        return records
    }

    private suspend fun aggregateStepDates(
        client: HealthConnectClient,
        dates: Set<LocalDate>,
        serverZone: ZoneId,
        now: Instant,
    ): List<WeightedRecord> {
        val result = mutableListOf<WeightedRecord>()
        for (localDate in dates.sorted()) {
            val dayStart = localDate.atStartOfDay(serverZone).toInstant()
            val dayEnd = localDate.plusDays(1).atStartOfDay(serverZone).toInstant()
            val queryEnd = minOf(dayEnd, now)
            if (!queryEnd.isAfter(dayStart)) continue
            val aggregate = client.aggregate(
                AggregateRequest(
                    metrics = setOf(StepsRecord.COUNT_TOTAL),
                    timeRangeFilter = TimeRangeFilter.between(dayStart, queryEnd),
                )
            )
            val total = aggregate[StepsRecord.COUNT_TOTAL] ?: continue
            val observedAt = if (dayEnd.isBefore(now)) dayEnd.minusMillis(1) else now
            val sourceId = "steps:${localDate}:${serverZone.id}"
            result += WeightedRecord(
                JSONObject()
                    .put("source_id", sourceId)
                    .put("daily_date", localDate.toString())
                    .put("aggregation_timezone", serverZone.id)
                    .put("total", total)
                    .put("observed_at", epochSeconds(observedAt)),
                1,
            )
        }
        return result
    }

    private fun affectedStepDates(
        records: List<StepsRecord>,
        serverZone: ZoneId,
        now: Instant,
    ): Set<LocalDate> {
        val today = now.atZone(serverZone).toLocalDate()
        val oldest = today.minusDays(HISTORY_DAYS - 1L)
        val dates = linkedSetOf<LocalDate>()
        for (record in records) {
            var cursor = record.startTime.atZone(serverZone).toLocalDate()
            val endProbe = if (record.endTime.isAfter(record.startTime)) {
                record.endTime.minusNanos(1)
            } else {
                record.startTime
            }
            val last = endProbe.atZone(serverZone).toLocalDate()
            while (!cursor.isAfter(last)) {
                if (!cursor.isBefore(oldest) && !cursor.isAfter(today)) dates += cursor
                cursor = cursor.plusDays(1)
            }
        }
        return dates
    }

    private fun heartRateJson(record: HeartRateRecord): WeightedRecord? {
        if (record.samples.isEmpty()) return null
        val samples = JSONArray()
        for (sample in record.samples) {
            samples.put(
                JSONObject()
                    .put("observed_at", epochSeconds(sample.time))
                    .put("bpm", sample.beatsPerMinute)
            )
        }
        return WeightedRecord(
            JSONObject()
                .put(
                    "source_id",
                    stableRecordId("heart_rate", record.metadata, record.startTime, record.endTime),
                )
                .put("start_at", epochSeconds(record.startTime))
                .put("end_at", epochSeconds(record.endTime))
                .put("samples", samples),
            record.samples.size,
        )
    }

    private fun spo2Json(record: OxygenSaturationRecord): WeightedRecord {
        return WeightedRecord(
            JSONObject()
                .put(
                    "source_id",
                    stableRecordId("spo2", record.metadata, record.time, record.time),
                )
                .put("observed_at", epochSeconds(record.time))
                .put("percentage", record.percentage.value),
            1,
        )
    }

    private fun sleepJson(record: SleepSessionRecord): WeightedRecord {
        val stages = JSONArray()
        for (stage in record.stages) {
            stages.put(
                JSONObject()
                    .put("start_at", epochSeconds(stage.startTime))
                    .put("end_at", epochSeconds(stage.endTime))
                    .put("stage", stageName(stage.stage))
            )
        }
        return WeightedRecord(
            JSONObject()
                .put(
                    "source_id",
                    stableRecordId("sleep", record.metadata, record.startTime, record.endTime),
                )
                .put("start_at", epochSeconds(record.startTime))
                .put("end_at", epochSeconds(record.endTime))
                .put("stages", stages),
            maxOf(1, record.stages.size),
        )
    }

    private fun postRecordBatches(
        arrayName: String,
        records: List<WeightedRecord>,
        base: String,
    ): Boolean {
        if (records.isEmpty()) return true
        val page = mutableListOf<WeightedRecord>()
        var leaves = 0

        fun flush(): Boolean {
            if (page.isEmpty()) return true
            val array = JSONArray()
            page.forEach { array.put(it.json) }
            val body = emptyBatchBody().put(arrayName, array)
            val response = postJson(base, "/api/biometrics/batch", body)
            if (!response.ok) return false
            rememberServerTimezone(response.dailyTimezone)
            page.clear()
            leaves = 0
            return true
        }

        for (record in records) {
            if (record.leaves > MAX_BATCH_LEAVES) {
                Log.e(
                    tag,
                    "$arrayName source record has ${record.leaves} leaves; " +
                        "cannot split one source record",
                )
                return false
            }
            if (page.isNotEmpty() && leaves + record.leaves > MAX_BATCH_LEAVES) {
                if (!flush()) return false
            }
            page += record
            leaves += record.leaves
        }
        return flush()
    }

    private fun bootstrapServerTimezone(base: String): ZoneId? {
        prefs.getString(KEY_SERVER_TIMEZONE, null)?.let { cached ->
            try {
                return ZoneId.of(cached)
            } catch (_: Exception) {
                prefs.edit().remove(KEY_SERVER_TIMEZONE).apply()
            }
        }
        val response = postJson(base, "/api/biometrics/batch", emptyBatchBody())
        if (!response.ok) return null
        val timezone = response.dailyTimezone ?: return null
        return try {
            ZoneId.of(timezone).also { rememberServerTimezone(timezone) }
        } catch (e: Exception) {
            Log.w(tag, "server returned invalid daily_timezone=$timezone")
            null
        }
    }

    private fun emptyBatchBody(): JSONObject {
        return JSONObject()
            .put("sent_at", epochSeconds(Instant.now()))
            .put("device_timezone", ZoneId.systemDefault().id)
            .put("heart_rate_records", JSONArray())
            .put("spo2_records", JSONArray())
            .put("sleep_sessions", JSONArray())
            .put("steps_daily", JSONArray())
    }

    private fun postJson(base: String, path: String, body: JSONObject): HttpResult {
        return try {
            val request = Request.Builder()
                .url(base + path)
                .post(body.toString().toRequestBody(jsonMedia))
                .build()
            http.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                val timezone = try {
                    if (text.isBlank()) null else JSONObject(text)
                        .optString("daily_timezone")
                        .takeIf { it.isNotBlank() }
                } catch (_: Exception) {
                    null
                }
                Log.i(
                    tag,
                    "POST $path -> ${response.code} " +
                        "leaves=${batchLeafCount(body)} timezone=$timezone",
                )
                if (!response.isSuccessful) {
                    Log.w(tag, "POST $path response=${text.take(300)}")
                }
                HttpResult(response.isSuccessful, response.code, timezone)
            }
        } catch (e: Exception) {
            Log.w(tag, "POST $path failed: ${e.message}")
            HttpResult(false, 0, null)
        }
    }

    private fun rememberServerTimezone(timezone: String?) {
        if (timezone.isNullOrBlank()) return
        try {
            ZoneId.of(timezone)
            prefs.edit().putString(KEY_SERVER_TIMEZONE, timezone).apply()
        } catch (_: Exception) {
            Log.w(tag, "ignored invalid daily_timezone=$timezone")
        }
    }

    private fun ensureServerIdentity(base: String) {
        val existing = prefs.getString(KEY_SERVER_BASE, null)
        if (existing == base) return
        prefs.edit().clear().putString(KEY_SERVER_BASE, base).commit()
        Log.i(tag, "Health Connect sync state reset for server=$base")
    }

    private fun backgroundReadSupported(client: HealthConnectClient): Boolean {
        return try {
            client.features.getFeatureStatus(
                HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND
            ) == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
        } catch (_: Exception) {
            false
        }
    }

    private fun stableRecordId(
        kind: String,
        metadata: Metadata,
        start: Instant,
        end: Instant,
    ): String {
        metadata.id.trim().takeIf { it.isNotEmpty() }?.let { return it }
        metadata.clientRecordId?.trim()?.takeIf { it.isNotEmpty() }?.let {
            return "client:$it"
        }
        return "$kind:${metadata.dataOrigin.packageName}:" +
            "${start.toEpochMilli()}:${end.toEpochMilli()}"
    }

    private fun stageName(stage: Int?): String {
        return when (stage) {
            SleepSessionRecord.STAGE_TYPE_AWAKE,
            SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED,
            SleepSessionRecord.STAGE_TYPE_OUT_OF_BED -> "awake"
            SleepSessionRecord.STAGE_TYPE_LIGHT -> "light"
            SleepSessionRecord.STAGE_TYPE_DEEP -> "deep"
            SleepSessionRecord.STAGE_TYPE_REM -> "rem"
            else -> "sleeping"
        }
    }

    private fun batchLeafCount(body: JSONObject): Int {
        var count = body.optJSONArray("spo2_records")?.length() ?: 0
        count += body.optJSONArray("steps_daily")?.length() ?: 0
        body.optJSONArray("heart_rate_records")?.let { records ->
            for (index in 0 until records.length()) {
                count += records.getJSONObject(index).optJSONArray("samples")?.length() ?: 0
            }
        }
        body.optJSONArray("sleep_sessions")?.let { sessions ->
            for (index in 0 until sessions.length()) {
                count += maxOf(
                    1,
                    sessions.getJSONObject(index).optJSONArray("stages")?.length() ?: 0,
                )
            }
        }
        return count
    }

    private fun epochSeconds(instant: Instant): Double = instant.toEpochMilli() / 1000.0

    private data class WeightedRecord(val json: JSONObject, val leaves: Int)
    private data class HttpResult(
        val ok: Boolean,
        val code: Int,
        val dailyTimezone: String?,
    )

    private enum class SyncOutcome { SUCCESS, EXPIRED, FAILED }

    private enum class HcKind(
        val wireName: String,
        val permission: String,
        val tokenKey: String,
        val recordType: KClass<out Record>,
        val arrayName: String,
    ) {
        HEART_RATE(
            "heart_rate",
            HealthPermission.getReadPermission(HeartRateRecord::class),
            "changes_token_heart_rate",
            HeartRateRecord::class,
            "heart_rate_records",
        ),
        SPO2(
            "spo2",
            HealthPermission.getReadPermission(OxygenSaturationRecord::class),
            "changes_token_spo2",
            OxygenSaturationRecord::class,
            "spo2_records",
        ),
        SLEEP(
            "sleep",
            HealthPermission.getReadPermission(SleepSessionRecord::class),
            "changes_token_sleep",
            SleepSessionRecord::class,
            "sleep_sessions",
        ),
        STEPS(
            "steps",
            HealthPermission.getReadPermission(StepsRecord::class),
            "changes_token_steps",
            StepsRecord::class,
            "steps_daily",
        ),
    }

    companion object {
        private const val PREFS_NAME = "health_connect_sync_v2"
        private const val KEY_SERVER_BASE = "server_base"
        private const val KEY_SERVER_TIMEZONE = "server_daily_timezone"
        private const val READ_PAGE_SIZE = 500
        private const val MAX_BATCH_LEAVES = 1000
        private const val HISTORY_DAYS = 14
        private const val FRESH_MINUTES = 15L

        private val PERMISSIONS_BY_KIND = linkedMapOf(
            "heart_rate" to HealthPermission.getReadPermission(HeartRateRecord::class),
            "spo2" to HealthPermission.getReadPermission(OxygenSaturationRecord::class),
            "sleep" to HealthPermission.getReadPermission(SleepSessionRecord::class),
            "steps" to HealthPermission.getReadPermission(StepsRecord::class),
        )

        @JvmStatic
        fun dataPermissions(): Set<String> = PERMISSIONS_BY_KIND.values.toSet()

        @JvmStatic
        fun requestedPermissions(context: Context): Set<String> {
            val result = dataPermissions().toMutableSet()
            val status = authorizationStatus(context)
            if (status.backgroundSupported) {
                result += HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND
            }
            return result
        }

        @JvmStatic
        fun authorizationStatus(context: Context): HealthAuthorizationStatus {
            return runBlocking {
                try {
                    if (
                        HealthConnectClient.getSdkStatus(context) !=
                        HealthConnectClient.SDK_AVAILABLE
                    ) {
                        return@runBlocking HealthAuthorizationStatus(
                            sdkAvailable = false,
                            grantedDataPermissions = emptySet(),
                            backgroundSupported = false,
                            backgroundGranted = false,
                        )
                    }
                    val client = HealthConnectClient.getOrCreate(context)
                    val granted = client.permissionController.getGrantedPermissions()
                    val backgroundSupported = try {
                        client.features.getFeatureStatus(
                            HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND
                        ) == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
                    } catch (_: Exception) {
                        false
                    }
                    HealthAuthorizationStatus(
                        sdkAvailable = true,
                        grantedDataPermissions = dataPermissions().filterTo(linkedSetOf()) {
                            it in granted
                        },
                        backgroundSupported = backgroundSupported,
                        backgroundGranted =
                            HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND in granted,
                    )
                } catch (_: Exception) {
                    HealthAuthorizationStatus(
                        sdkAvailable = false,
                        grantedDataPermissions = emptySet(),
                        backgroundSupported = false,
                        backgroundGranted = false,
                    )
                }
            }
        }
    }
}
