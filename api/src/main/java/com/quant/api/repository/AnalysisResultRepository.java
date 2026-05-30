package com.quant.api.repository;

import com.quant.api.domain.AnalysisResult;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.util.List;

public interface AnalysisResultRepository extends JpaRepository<AnalysisResult, Long> {

    @Query("""
        SELECT r FROM AnalysisResult r
        WHERE r.run.id = (
            SELECT MAX(run.id) FROM AnalysisRun run
            WHERE run.market = :market AND run.horizon = :horizon AND run.status = 'completed'
        )
        ORDER BY r.rank
        """)
    List<AnalysisResult> findLatestByMarketAndHorizon(String market, String horizon);

    @Query("""
        SELECT r FROM AnalysisResult r
        WHERE r.run.id = :runId
        ORDER BY r.rank
        """)
    List<AnalysisResult> findByRunId(Long runId);
}
