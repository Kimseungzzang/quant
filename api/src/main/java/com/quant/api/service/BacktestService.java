package com.quant.api.service;

import com.quant.api.domain.BacktestResult;
import com.quant.api.repository.BacktestResultRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
@RequiredArgsConstructor
@Transactional(readOnly = true)
public class BacktestService {

    private final BacktestResultRepository backtestResultRepository;

    public List<BacktestResult> getByMarket(String market, int limit) {
        return backtestResultRepository.findByMarketOrderByRunAtDesc(market, PageRequest.of(0, limit));
    }

    public List<BacktestResult> getByStockCode(String stockCode, int limit) {
        return backtestResultRepository.findByStockCodeOrderByRunAtDesc(stockCode, PageRequest.of(0, limit));
    }

    public List<BacktestResult> getByAnalysisRun(Long runId) {
        return backtestResultRepository.findByAnalysisRunIdOrderByTotalReturnPctDesc(runId);
    }
}
