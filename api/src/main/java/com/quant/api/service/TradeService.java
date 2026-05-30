package com.quant.api.service;

import com.quant.api.dto.PnlChartDto;
import com.quant.api.dto.PnlSummaryDto;
import com.quant.api.dto.TradeDto;
import com.quant.api.repository.PortfolioSnapshotRepository;
import com.quant.api.repository.TradeRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.List;

@Service
@RequiredArgsConstructor
@Transactional(readOnly = true)
public class TradeService {

    private final TradeRepository tradeRepository;
    private final PortfolioSnapshotRepository snapshotRepository;

    public Page<TradeDto> getTrades(String mode, String market, Pageable pageable) {
        if (market != null && !market.isBlank()) {
            return tradeRepository.findByMarketAndModeOrderByTradedAtDesc(market, mode, pageable)
                .map(TradeDto::from);
        }
        return tradeRepository.findByModeOrderByTradedAtDesc(mode, pageable)
            .map(TradeDto::from);
    }

    public PnlSummaryDto getPnlSummary(String mode) {
        BigDecimal totalPnl    = tradeRepository.sumRealizedPnl(mode);
        long closed            = tradeRepository.countClosedTrades(mode);
        long wins              = tradeRepository.countWinningTrades(mode);
        double winRate         = closed > 0 ? (double) wins / closed * 100 : 0;
        BigDecimal avgPnl      = closed > 0
            ? totalPnl.divide(BigDecimal.valueOf(closed), 4, RoundingMode.HALF_UP)
            : BigDecimal.ZERO;

        return PnlSummaryDto.builder()
            .totalRealizedPnl(totalPnl)
            .totalTrades(closed)
            .winningTrades(wins)
            .winRate(Math.round(winRate * 10.0) / 10.0)
            .avgPnlPerTrade(avgPnl)
            .build();
    }

    public List<PnlChartDto> getPnlChart(String mode, int days) {
        LocalDate since = LocalDate.now().minusDays(days);
        return snapshotRepository
            .findByModeAndSnapshotDateGreaterThanEqualOrderBySnapshotDateAsc(mode, since)
            .stream()
            .map(PnlChartDto::from)
            .toList();
    }
}
