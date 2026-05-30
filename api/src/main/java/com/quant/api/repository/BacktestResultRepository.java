package com.quant.api.repository;

import com.quant.api.domain.BacktestResult;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface BacktestResultRepository extends JpaRepository<BacktestResult, Long> {

    List<BacktestResult> findByStockCodeOrderByRunAtDesc(String stockCode, Pageable pageable);

    List<BacktestResult> findByMarketOrderByRunAtDesc(String market, Pageable pageable);

    List<BacktestResult> findByAnalysisRunIdOrderByTotalReturnPctDesc(Long analysisRunId);
}
