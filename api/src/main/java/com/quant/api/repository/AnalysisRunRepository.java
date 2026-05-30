package com.quant.api.repository;

import com.quant.api.domain.AnalysisRun;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AnalysisRunRepository extends JpaRepository<AnalysisRun, Long> {

    List<AnalysisRun> findByMarketAndHorizonAndStatusOrderByRunAtDesc(
        String market,
        String horizon,
        String status
    );
}
