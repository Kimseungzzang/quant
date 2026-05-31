package com.quant.api.service;

import com.quant.api.dto.DailyReportDto;
import com.quant.api.dto.PnlChartDto;
import com.quant.api.dto.PnlSummaryDto;
import com.quant.api.dto.PositionDto;
import com.quant.api.dto.StockPerformanceDto;
import com.quant.api.dto.TradeDto;
import com.quant.api.repository.PositionRepository;
import com.quant.api.repository.PortfolioSnapshotRepository;
import com.quant.api.repository.TradeRepository;
import jakarta.persistence.EntityManager;
import jakarta.persistence.TypedQuery;
import lombok.RequiredArgsConstructor;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageImpl;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Service
@RequiredArgsConstructor
@Transactional(readOnly = true)
public class TradeService {

    private final TradeRepository tradeRepository;
    private final PortfolioSnapshotRepository snapshotRepository;
    private final PositionRepository positionRepository;
    private final EntityManager entityManager;

    public Page<TradeDto> getTrades(String mode, String market, Pageable pageable) {
        if (market != null && !market.isBlank()) {
            return tradeRepository.findByMarketAndModeOrderByTradedAtDesc(market, mode, pageable)
                .map(TradeDto::from);
        }
        return tradeRepository.findByModeOrderByTradedAtDesc(mode, pageable)
            .map(TradeDto::from);
    }

    public Page<TradeDto> searchTrades(
        String mode, String market, String stockCode, String period, Pageable pageable
    ) {
        OffsetDateTime since = since(period);
        String normalizedMarket = blankToNull(market);
        String normalizedStock = blankToNull(stockCode);
        StringBuilder jpql = new StringBuilder("SELECT t FROM Trade t WHERE t.mode = :mode");
        StringBuilder countJpql = new StringBuilder("SELECT COUNT(t) FROM Trade t WHERE t.mode = :mode");
        if (normalizedMarket != null) {
            jpql.append(" AND t.market = :market");
            countJpql.append(" AND t.market = :market");
        }
        if (normalizedStock != null) {
            jpql.append(" AND t.stockCode = :stockCode");
            countJpql.append(" AND t.stockCode = :stockCode");
        }
        if (since != null) {
            jpql.append(" AND t.tradedAt >= :since");
            countJpql.append(" AND t.tradedAt >= :since");
        }
        jpql.append(" ORDER BY t.tradedAt DESC");

        TypedQuery<com.quant.api.domain.Trade> query = entityManager.createQuery(
            jpql.toString(), com.quant.api.domain.Trade.class);
        TypedQuery<Long> countQuery = entityManager.createQuery(countJpql.toString(), Long.class);
        query.setParameter("mode", mode);
        countQuery.setParameter("mode", mode);
        if (normalizedMarket != null) {
            query.setParameter("market", normalizedMarket);
            countQuery.setParameter("market", normalizedMarket);
        }
        if (normalizedStock != null) {
            query.setParameter("stockCode", normalizedStock);
            countQuery.setParameter("stockCode", normalizedStock);
        }
        if (since != null) {
            query.setParameter("since", since);
            countQuery.setParameter("since", since);
        }

        query.setFirstResult((int) pageable.getOffset());
        query.setMaxResults(pageable.getPageSize());
        return new PageImpl<>(query.getResultList(), pageable, countQuery.getSingleResult())
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

    public List<PositionDto> getPositions(String mode) {
        return positionRepository.findByModeAndQuantityGreaterThanOrderByUpdatedAtDesc(mode, 0)
            .stream()
            .map(PositionDto::from)
            .toList();
    }

    public List<StockPerformanceDto> getStockPerformance(String mode, String period) {
        return closedTrades(mode, period)
            .stream()
            .collect(
                LinkedHashMap<String, Acc>::new,
                (map, trade) -> {
                    String key = trade.getStockCode() + "\t" + trade.getStockName();
                    map.computeIfAbsent(key, k -> new Acc(trade.getStockCode(), trade.getStockName()))
                        .add(trade.getRealizedPnl());
                },
                Map::putAll
            )
            .values()
            .stream()
            .map(Acc::toStockPerformance)
            .sorted(Comparator.comparing(StockPerformanceDto::getTotalPnl).reversed())
            .toList();
    }

    public List<DailyReportDto> getDailyReports(String mode, String period) {
        return closedTrades(mode, period)
            .stream()
            .collect(
                LinkedHashMap<LocalDate, Acc>::new,
                (map, trade) -> {
                    LocalDate day = trade.getTradedAt()
                        .atZoneSameInstant(ZoneId.systemDefault())
                        .toLocalDate();
                    map.computeIfAbsent(day, k -> new Acc(null, null)).add(trade.getRealizedPnl());
                },
                Map::putAll
            )
            .entrySet()
            .stream()
            .map(e -> e.getValue().toDailyReport(e.getKey()))
            .sorted(Comparator.comparing(DailyReportDto::getDate).reversed())
            .toList();
    }

    private static OffsetDateTime since(String period) {
        if (period == null || period.isBlank() || period.equals("all")) {
            return null;
        }
        LocalDate today = LocalDate.now();
        LocalDate start = switch (period) {
            case "today" -> today;
            case "week" -> today.minusDays(7);
            case "month" -> today.minusMonths(1);
            case "quarter" -> today.minusMonths(3);
            default -> null;
        };
        return start == null ? null : start.atStartOfDay(ZoneId.systemDefault()).toOffsetDateTime();
    }

    private List<com.quant.api.domain.Trade> closedTrades(String mode, String period) {
        OffsetDateTime start = since(period);
        if (start == null) {
            return tradeRepository.findBySideAndModeOrderByTradedAtDesc("SELL", mode);
        }
        return tradeRepository.findBySideAndModeAndTradedAtGreaterThanEqualOrderByTradedAtDesc(
            "SELL", mode, start
        );
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value;
    }

    private static class Acc {
        private final String stockCode;
        private final String stockName;
        private long trades;
        private long wins;
        private BigDecimal total = BigDecimal.ZERO;
        private BigDecimal max;
        private BigDecimal min;

        Acc(String stockCode, String stockName) {
            this.stockCode = stockCode;
            this.stockName = stockName;
        }

        void add(BigDecimal pnl) {
            BigDecimal value = pnl != null ? pnl : BigDecimal.ZERO;
            trades++;
            if (value.compareTo(BigDecimal.ZERO) > 0) {
                wins++;
            }
            total = total.add(value);
            max = max == null || value.compareTo(max) > 0 ? value : max;
            min = min == null || value.compareTo(min) < 0 ? value : min;
        }

        StockPerformanceDto toStockPerformance() {
            return StockPerformanceDto.builder()
                .stockCode(stockCode)
                .stockName(stockName)
                .tradePairs(trades)
                .wins(wins)
                .winRate(winRate())
                .totalPnl(total)
                .avgPnl(avg())
                .maxPnl(max != null ? max : BigDecimal.ZERO)
                .minPnl(min != null ? min : BigDecimal.ZERO)
                .build();
        }

        DailyReportDto toDailyReport(LocalDate date) {
            return DailyReportDto.builder()
                .date(date)
                .tradePairs(trades)
                .wins(wins)
                .losses(trades - wins)
                .winRate(winRate())
                .totalPnl(total)
                .maxPnl(max != null ? max : BigDecimal.ZERO)
                .minPnl(min != null ? min : BigDecimal.ZERO)
                .build();
        }

        private BigDecimal avg() {
            return trades > 0
                ? total.divide(BigDecimal.valueOf(trades), 4, RoundingMode.HALF_UP)
                : BigDecimal.ZERO;
        }

        private double winRate() {
            double value = trades > 0 ? (double) wins / trades * 100 : 0;
            return Math.round(value * 10.0) / 10.0;
        }
    }
}
